# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License;
# you may not use this file except in compliance with the Elastic License.

"""Load rule metadata transform between rule and api formats."""
import functools
import glob
import io
import json
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List

import click
import pytoml
import requests

from .mappings import RtaMappings
from .rule import RULES_DIR, Rule
from .schemas import CurrentSchema
from .utils import get_path, cached, unzip


RTA_DIR = get_path("rta")
FILE_PATTERN = r'^([a-z0-9_])+\.(json|toml)$'


def mock_loader(f):
    """Mock rule loader."""
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        finally:
            load_rules.clear()

    return wrapped


def reset():
    """Clear all rule caches."""
    load_rule_files.clear()
    load_rules.clear()
    get_rule.clear()
    filter_rules.clear()


@cached
def load_rule_files(verbose=True, paths=None):
    """Load the rule YAML files, but without parsing the EQL query portion."""
    file_lookup: Dict[str, dict] = {}

    if verbose:
        print("Loading rules from {}".format(RULES_DIR))

    if paths is None:
        paths = sorted(glob.glob(os.path.join(RULES_DIR, '**', '*.toml'), recursive=True))

    for rule_file in paths:
        try:
            # use pytoml instead of toml because of annoying bugs
            # https://github.com/uiri/toml/issues/152
            # might also be worth looking at https://github.com/sdispater/tomlkit
            with io.open(rule_file, "r", encoding="utf-8") as f:
                file_lookup[rule_file] = pytoml.load(f)
        except Exception:
            print(u"Error loading {}".format(rule_file))
            raise

    if verbose:
        print("Loaded {} rules".format(len(file_lookup)))
    return file_lookup


@cached
def load_rule_contents_from_release(path: str) -> Dict[str, dict]:
    """Load rule files from local release package. Metadata will not be accurate and will use defaults."""
    path = Path(path)
    rules_path = path.joinpath('rules')
    rule_files = rules_path.glob('*.json')
    rules = [json.loads(r.read_text()) for r in rule_files]
    return {r['rule_id']: r for r in rules}


@cached
def load_rule_contents_from_github_release(version: str) -> Dict[str, Rule]:
    """Load rule files from GitHub release."""
    zip_url = f'https://api.github.com/repos/elastic/detection-rules/zipball/v{version}'
    response = requests.get(zip_url)
    rules = {}

    with unzip(response.content) as archive:
        base_directory = archive.namelist()[0]

        for name in archive.namelist():
            if name.startswith(f'{base_directory}rules/') and name.endswith('.toml'):
                rule = pytoml.loads(archive.read(name))
                rules[rule['rule']['rule_id']] = rule

    return rules


@cached
def load_rules(file_lookup=None, verbose=True, error=True):
    """Load all the rules from toml files."""
    file_lookup = file_lookup or load_rule_files(verbose=verbose)

    failed = False
    rules = []  # type: list[Rule]
    errors = []
    queries = []
    query_check_index = []
    rule_ids = set()
    rule_names = set()

    for rule_file, rule_contents in file_lookup.items():
        try:
            rule = Rule(rule_file, rule_contents)

            if rule.id in rule_ids:
                existing = next(r for r in rules if r.id == rule.id)
                raise KeyError(f'{rule.path} has duplicate ID with \n{existing.path}')

            if rule.name in rule_names:
                existing = next(r for r in rules if r.name == rule.name)
                raise KeyError(f'{rule.path} has duplicate name with \n{existing.path}')

            parsed_query = rule.parsed_query
            if parsed_query is not None:
                # duplicate logic is ok across query and threshold rules
                threshold = rule.contents.get('threshold', {})
                duplicate_key = (parsed_query, rule.type, threshold.get('field'), threshold.get('value'))
                query_check_index.append(rule)

                if duplicate_key in queries:
                    existing = query_check_index[queries.index(duplicate_key)]
                    raise KeyError(f'{rule.path} has duplicate query with \n{existing.path}')

                queries.append(duplicate_key)

            if not re.match(FILE_PATTERN, os.path.basename(rule.path)):
                raise ValueError(f'{rule.path} does not meet rule name standard of {FILE_PATTERN}')

            rules.append(rule)
            rule_ids.add(rule.id)
            rule_names.add(rule.name)

        except Exception as e:
            failed = True
            err_msg = "Invalid rule file in {}\n{}".format(rule_file, click.style(e.args[0], fg='red'))
            errors.append(err_msg)
            if error:
                if verbose:
                    print(err_msg)
                raise e

    if failed:
        if verbose:
            for e in errors:
                print(e)

    return OrderedDict([(rule.id, rule) for rule in sorted(rules, key=lambda r: r.name)])


@cached
def get_rule(rule_id=None, rule_name=None, file_name=None, verbose=True):
    """Get a rule based on its id."""
    rules_lookup = load_rules(verbose=verbose)
    if rule_id is not None:
        return rules_lookup.get(rule_id)

    for rule in rules_lookup.values():  # type: Rule
        if rule.name == rule_name:
            return rule
        elif rule.path == file_name:
            return rule


def get_rule_name(rule_id, verbose=True):
    """Get the name of a rule given the rule id."""
    rule = get_rule(rule_id, verbose=verbose)
    if rule:
        return rule.name


def get_file_name(rule_id, verbose=True):
    """Get the file path that corresponds to a rule."""
    rule = get_rule(rule_id, verbose=verbose)
    if rule:
        return rule.path


def get_rule_contents(rule_id, verbose=True):
    """Get the full contents for a rule_id."""
    rule = get_rule(rule_id, verbose=verbose)
    if rule:
        return rule.contents


@cached
def filter_rules(metadata_field, value):
    """Filter rules based on the metadata."""
    return [rule for rule in load_rules(verbose=False).values() if rule.metadata.get(metadata_field, '') == value]


def get_production_rules(verbose=False, include_deprecated=False) -> List[Rule]:
    """Get rules with a maturity of production."""
    from .packaging import filter_rule

    maturity = ['production']
    if include_deprecated:
        maturity.append('deprecated')
    return list(filter(lambda rule: filter_rule(rule, dict(maturity=maturity)), load_rules(verbose=verbose).values()))


def find_unneeded_defaults(rule):
    """Remove values that are not required in the schema which are set with default values."""
    schema = CurrentSchema.get_schema(rule.type)
    props = schema['properties']
    unrequired_defaults = [p for p in props if p not in schema['required'] and props[p].get('default')]
    default_matches = {p: rule.contents[p] for p in unrequired_defaults
                       if rule.contents.get(p) and rule.contents[p] == props[p]['default']}
    return default_matches


rta_mappings = RtaMappings()


__all__ = (
    "load_rule_files",
    "load_rules",
    "load_rule_files",
    "get_file_name",
    "get_production_rules",
    "get_rule",
    "filter_rules",
    "get_rule_name",
    "get_rule_contents",
    "reset",
    "rta_mappings"
)
