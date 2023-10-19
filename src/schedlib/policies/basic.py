#!/usr/bin/env python3

import datetime as dt
from abc import ABC, abstractmethod
from typing import List 
from dataclasses import dataclass
from .. import core, utils, commands as cmd, instrument as inst, rules as ru, source as src, config as cfg


@dataclass(frozen=True)
class BasePolicy(core.Policy, ABC):
    """we split the policy into two parts: transform and merge where
    transform are the part that preserves nested structure and merge
    is the part that flattens the nested structure into a single
    sequence. This is mostly for visualization purposes, so that we
    preserve the nested structure for the user to see, but we can
    also flatten the structure for the scheduler to consume."""

    @abstractmethod
    def transform(self, blocks: core.BlocksTree) -> core.BlocksTree: ...

    @abstractmethod
    def merge(self, blocks: core.BlocksTree) -> core.Blocks: ...

    def apply(self, blocks: core.BlocksTree) -> core.Blocks:
        """main interface"""
        blocks = self.transform(blocks)
        blocks = self.merge(blocks)
        return blocks

    @abstractmethod
    def seq2cmd(self, seq: core.Blocks) -> cmd.Command: ...


@dataclass(frozen=True)
class BasicPolicy(BasePolicy):
    rules: core.RuleSet
    master_schedule: str
    calibration_targets: List[str]
    soft_targets: List[str]

    def make_rule(self, rule_name: str, **kwargs) -> core.Rule:
        # caller kwargs take precedence
        print(self.rules)
        if not kwargs:
            assert rule_name in self.rules, f"Rule {rule_name} not found in rules config"
            kwargs = self.rules[rule_name]  
        return ru.make_rule(rule_name, **kwargs)

    def init_seqs(self, t0: dt.datetime, t1: dt.datetime) -> core.BlocksTree:
        master = inst.parse_sequence_from_toast(self.master_schedule)
        calibration = {k: src.source_gen_seq(k, t0, t1) for k in self.calibration_targets}
        soft = {k: src.source_gen_seq(k, t0, t1) for k in self.soft_targets}
        blocks = {
            'master': master,
            'sources': {
                'calibration': calibration,
                'soft': soft,
            }
        }
        return core.seq_trim(blocks, t0, t1)

    def transform(self, blocks: core.BlocksTree) -> core.BlocksTree:
        # sun avoidance for all
        blocks = self.make_rule('sun-avoidance')(blocks)

        # plan for sources
        blocks['sources'] = self.make_rule('make-source-plan')(blocks['sources'])

        # add calibration targets
        cal_blocks = blocks['sources']['calibration']
        if 'day-mod' in self.rules:
            cal_blocks = self.make_rule('day-mod')(cal_blocks)
        if 'drift-mode' in self.rules:
            cal_blocks = self.make_rule('drift-mode')(cal_blocks)
        if 'calibration-min-duration' in self.rules:
            cal_blocks = self.make_rule(
                'min-duration',
                **self.rules['calibration-min-duration']
            )(cal_blocks)
        if 'alt-range' in self.rules:
            cal_blocks = self.make_rule('alt-range')(cal_blocks)

        # actually turn observation windows into source scans: need some random
        # numbers to rephase each source scan in an observing window. we will
        # use a daily static key, producing exactly the same sequence of random
        # numbers when the date is the same
        if len(core.seq_flatten(cal_blocks)) > 0:
            first_block = core.seq_sort(cal_blocks, flatten=True)[0]
            keys = utils.daily_static_key(first_block.t0).split(len(cal_blocks))
            for srcname, key in zip(cal_blocks, keys):
                cal_blocks[srcname] = self.make_rule(
                    'make-source-scan',
                    rng_key=key,
                    **self.rules['make-source-scan']
                )(cal_blocks[srcname])
        blocks['sources']['calibration'] = cal_blocks
        return blocks

    def merge(self, blocks: core.BlocksTree) -> core.Blocks:
        # merge all calibration sources into main sequence
        blocks = core.seq_merge(blocks['master'], blocks['sources']['calibration'], flatten=True)
        if 'min-duration' in self.rules:
            blocks = self.make_rule('min-duration')(blocks)
        return core.seq_sort(blocks)

    def block2cmd(self, block: core.Block):
        if isinstance(block, inst.ScanBlock):
            return cmd.CompositeCommand([
                    f"# {block.name}",
                    cmd.Goto(block.az, block.alt),
                    cmd.BiasDets(),
                    cmd.Wait(block.t0),
                    cmd.BiasStep(),
                    cmd.Scan(block.name, block.t1, block.throw),
                    cmd.BiasStep(),
                    "",
            ])

    def seq2cmd(self, seq: core.Blocks):
        """map a scan to a command"""
        commands = core.seq_flatten(core.seq_map(self.block2cmd, seq))
        commands = [cmd.Preamble()] + commands
        return cmd.CompositeCommand(commands)