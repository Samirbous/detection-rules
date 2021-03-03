# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License;
# you may not use this file except in compliance with the Elastic License.

"""Packaging and preparation for releases."""
import base64
import datetime
import hashlib
import json
import os
import shutil
from collections import defaultdict, OrderedDict
from pathlib import Path
from typing import List, OrderedDict as OrderedDictType

import click

from . import rule_loader
from .misc import JS_LICENSE, cached
from .rule import Rule  # noqa: F401
from .schemas import Changelog, CurrentSchema
from .utils import get_path, get_etc_path, load_etc_dump, save_etc_dump

RELEASE_DIR = get_path("releases")
PACKAGE_FILE = get_etc_path('packages.yml')
NOTICE_FILE = get_path('NOTICE.txt')
CHANGELOG_FILE = Path(get_etc_path('rules-changelog.json'))


def filter_rule(rule: Rule, config_filter: dict, exclude_fields: dict = None) -> bool:
    """Filter a rule based off metadata and a package configuration."""
    flat_rule = rule.flattened_contents
    for key, values in config_filter.items():
        if key not in flat_rule:
            return False

        values = set([v.lower() if isinstance(v, str) else v for v in values])
        rule_value = flat_rule[key]

        if isinstance(rule_value, list):
            rule_values = {v.lower() if isinstance(v, str) else v for v in rule_value}
        else:
            rule_values = {rule_value.lower() if isinstance(rule_value, str) else rule_value}

        if len(rule_values & values) == 0:
            return False

    exclude_fields = exclude_fields or {}
    for index, fields in exclude_fields.items():
        if rule.unique_fields and (rule.contents['index'] == index or index == 'any'):
            if set(rule.unique_fields) & set(fields):
                return False

    return True


@cached
def load_versions(current_versions: dict = None):
    """Load the versions file."""
    return current_versions or load_etc_dump('version.lock.json')


def manage_versions(rules: List[Rule], deprecated_rules: list = None, current_versions: dict = None,
                    exclude_version_update=False, add_new=True, save_changes=False, verbose=True) -> (list, list, list):
    """Update the contents of the version.lock file and optionally save changes."""
    new_rules = {}
    changed_rules = []

    current_versions = load_versions(current_versions)

    for rule in rules:
        # it is a new rule, so add it if specified, and add an initial version to the rule
        if rule.id not in current_versions:
            new_rules[rule.id] = {'rule_name': rule.name, 'version': 1, 'sha256': rule.get_hash()}
            rule.contents['version'] = 1
        else:
            version_lock_info = current_versions.get(rule.id)
            version = version_lock_info['version']
            rule_hash = rule.get_hash()

            # if it has been updated, then we need to bump the version info and optionally save the changes later
            if rule_hash != version_lock_info['sha256']:
                rule.contents['version'] = version + 1

                if not exclude_version_update:
                    version_lock_info['version'] = rule.contents['version']

                version_lock_info.update(sha256=rule_hash, rule_name=rule.name)
                changed_rules.append(rule.id)
            else:
                rule.contents['version'] = version

    # manage deprecated rules
    newly_deprecated = []
    rule_deprecations = {}

    if deprecated_rules:
        rule_deprecations = load_etc_dump('deprecated_rules.json')

        deprecation_date = str(datetime.date.today())

        for rule in deprecated_rules:
            if rule.id not in rule_deprecations:
                rule_deprecations[rule.id] = {
                    'rule_name': rule.name,
                    'deprecation_date': deprecation_date,
                    'stack_version': CurrentSchema.STACK_VERSION
                }
                newly_deprecated.append(rule.id)

    # update the document with the new rules
    if new_rules or changed_rules or newly_deprecated:
        if verbose:
            click.echo('Rule hash changes detected!')

        if save_changes:
            if changed_rules or (new_rules and add_new):
                current_versions.update(new_rules if add_new else {})
                current_versions = OrderedDict(sorted(current_versions.items(), key=lambda x: x[1]['rule_name']))

                save_etc_dump(current_versions, 'version.lock.json')

                if verbose:
                    click.echo('Updated version.lock.json file')

            if newly_deprecated:
                save_etc_dump(OrderedDict(sorted(rule_deprecations.items(), key=lambda e: e[1]['rule_name'])),
                              'deprecated_rules.json')

                if verbose:
                    click.echo('Updated deprecated_rules.json file')

            # only modify the global changelog _after_ versions are locked
            ChangelogMgmt.update_and_lock(rules)
        else:
            if verbose:
                click.echo('run `build-release --update-version-lock` to update the version.lock.json and '
                           'deprecated_rules.json files')

        if verbose:
            if changed_rules:
                click.echo(f' - {len(changed_rules)} changed rule version(s)')
            if new_rules:
                click.echo(f' - {len(new_rules)} new rule version addition(s)')
            if newly_deprecated:
                click.echo(f' - {len(newly_deprecated)} newly deprecated rule(s)')

    return changed_rules, list(new_rules), newly_deprecated


def versions_locked(rules: List[Rule] = None) -> bool:
    """Check if the version.lock file is fully reconciled with the current state of all rules."""
    rules = rules or rule_loader.get_production_rules(include_deprecated=True)
    versions = load_versions(bypass_cache=True)

    for rule in rules:
        if rule.id not in versions:
            return False
        elif rule.get_hash() != versions[rule.id]['sha256']:
            return False
    return True


class ChangelogMgmt:
    """Manage the global etc/rules-changelog."""

    @classmethod
    def initialize_rule_changelogs(cls, rules: List[Rule], force=False, flush=False):
        """Setup local rule logs within rules which have none defined."""
        import time

        versions = load_versions()

        for rule in rules:
            if rule.metadata.get('changelog') and not force:
                continue

            if flush:
                rule.metadata.pop('changelog', None)

            if rule.id in versions:
                rule.append_changelog_entry('_version_locked_')

                if rule.metadata['maturity'] == 'deprecated':
                    rule.append_changelog_entry('_deprecated_')
                elif rule.get_hash() != versions[rule.id]['sha256']:
                    rule.append_changelog_entry('some arbitrary change')
            else:
                rule.append_changelog_entry('_rule_created_')

            rule.metadata['updated_date'] = time.strftime('%Y/%m/%d')
            rule.save(as_rule=True)

    @classmethod
    def load_changelog_object(cls, path: Path = CHANGELOG_FILE) -> Changelog:
        changelog = Changelog.Schema().load(load_etc_dump(path))
        return changelog

    @classmethod
    def load_changelog(cls, path: Path = CHANGELOG_FILE) -> OrderedDictType:
        return OrderedDict(cls.load_changelog_object(path).dump())['changelog']

    @classmethod
    def update_and_lock(cls, rules: List[Rule], save_path: Path = CHANGELOG_FILE, dry_run=False):
        """Transfers rule changelogs to the global changelog and then inserts rule changelog entry."""
        from .schemas.changelog import ChangelogEntry

        changelog = cls.load_changelog()

        for rule in rules:
            rule_changelog = rule.metadata.get('changelog') if dry_run else rule.metadata.pop('changelog', [])
            # ignore defaults
            default_changes = ('_version_locked_', '_rule_created_')
            changes = [c for c in rule_changelog if c['message'] not in default_changes]

            if changes:
                global_entry = changelog.setdefault(rule.id, [])
                entry = ChangelogEntry.Schema().load({
                    'changes': changes,
                    'minimum_kibana_version': rule.metadata['minimum_kibana_version'],
                    'rule_version': rule.get_version()
                })

                global_entry.append(entry.dump())

                if dry_run:
                    click.echo(f'{rule.id} - {rule.name}:\n{json.dumps(global_entry, indent=2, sort_keys=True)}')
                else:
                    # a deprecated rule will need a rule changelog _deprecated_ entry until it is locked in a permanent
                    #   global rule changelog _deprecated_ entry
                    if not rule.metadata['maturity'] == 'deprecated':
                        rule.append_changelog_entry('_version_locked_')

                    rule.save(as_rule=True)
            else:
                # put back changelog for unchanged rules
                rule.metadata['changelog'] = rule_changelog

        if not dry_run:
            pass
            # once a rule changelog is popped from rule.metadata, it will fail unit tests unless the version lock is
            #   updated and saved
            if save_path == CHANGELOG_FILE:
                assert versions_locked(rules), 'Rule versions must be locked before locking the primary changelog'

            save_etc_dump({'changelog': changelog}, str(save_path))

    @staticmethod
    def _filter_entry(entry: dict, filter_config: dict) -> dict:
        """Filter changelog entries."""

        return entry

    @classmethod
    def markdown_from_file(cls, path: Path, rule_ids: list = None, filter_config: dict = None) -> str:
        """Generate a markdown formatted version of the global changelog."""
        # generate links for pulls
        # changelog = {k: v for k, v in cls.load_changelog().items() if k in rule_ids}
        # repo_url = 'https://github.com/elastic/detection-rules/pull/'
        # markdown = {}
        #
        # if filter_config:
        #     changelog = list(filter(lambda rule_entry: cls._filter_entry(rule_entry, filter_config), changelog.items()))  # noqa:E501
        #
        # for rule_id, rule_changelog in changelog.items():
        #     for entry in rule_changelog:
        #         # pr_link = ''
        #         changes = [f'{c["date"]} ([{c["pull_request"]}]({repo_url + c["pull_request"]})) {c["change"]}'
        #                    for c in entry['changes']]
        #         entry['changes'] = changes
        #
        # path.write_text(markdown)
        # return markdown

    @classmethod
    def markdown_from_rules(cls, rules: List[Rule]) -> str:
        """Generate markdown changelog from provided rules local changelogs."""


class Package(object):
    """Packaging object for siem rules and releases."""

    def __init__(self, rules: List[Rule], name, deprecated_rules: List[Rule] = None, release=False,
                 current_versions: dict = None, min_version: int = None, max_version: int = None,
                 update_version_lock=False, registry_data: dict = None):
        """Initialize a package."""
        self.rules = [r.copy() for r in rules]
        self.name = name
        self.deprecated_rules = [r.copy() for r in deprecated_rules or []]
        self.release = release
        self.registry_data = registry_data or {}

        self.changed_rule_ids, self.new_rules_ids, self.removed_rule_ids = self._add_versions(current_versions,
                                                                                              update_version_lock)

        if min_version or max_version:
            self.rules = [r for r in self.rules
                          if (min_version or 0) <= r.contents['version'] <= (max_version or r.contents['version'])]

    def _add_versions(self, current_versions, update_versions_lock=False):
        """Add versions to rules at load time."""
        return manage_versions(self.rules, deprecated_rules=self.deprecated_rules, current_versions=current_versions,
                               save_changes=update_versions_lock)

    @staticmethod
    def _package_notice_file(save_dir):
        """Convert and save notice file with package."""
        with open(NOTICE_FILE, 'rt') as f:
            notice_txt = f.read()

        with open(os.path.join(save_dir, 'notice.ts'), 'wt') as f:
            commented_notice = [f' * {line}'.rstrip() for line in notice_txt.splitlines()]
            lines = ['/* eslint-disable @kbn/eslint/require-license-header */', '', '/* @notice']
            lines = lines + commented_notice + [' */', '']
            f.write('\n'.join(lines))

    def _package_index_file(self, save_dir):
        """Convert and save index file with package."""
        sorted_rules = sorted(self.rules, key=lambda k: (k.metadata['creation_date'], os.path.basename(k.path)))
        comments = [
            '// Auto generated file from either:',
            '// - scripts/regen_prepackage_rules_index.sh',
            '// - detection-rules repo using CLI command build-release',
            '// Do not hand edit. Run script/command to regenerate package information instead',
        ]
        rule_imports = [f"import rule{i} from './{os.path.splitext(os.path.basename(r.path))[0] + '.json'}';"
                        for i, r in enumerate(sorted_rules, 1)]
        const_exports = ['export const rawRules = [']
        const_exports.extend(f"  rule{i}," for i, _ in enumerate(sorted_rules, 1))
        const_exports.append("];")
        const_exports.append("")

        index_ts = [JS_LICENSE, ""]
        index_ts.extend(comments)
        index_ts.append("")
        index_ts.extend(rule_imports)
        index_ts.append("")
        index_ts.extend(const_exports)

        with open(os.path.join(save_dir, 'index.ts'), 'wt') as f:
            f.write('\n'.join(index_ts))

    def save_release_files(self, directory, changed_rules, new_rules, removed_rules):
        """Release a package."""
        summary, changelog = self.generate_summary_and_changelog(changed_rules, new_rules, removed_rules)

        with open(os.path.join(directory, f'{self.name}-summary.txt'), 'w') as f:
            f.write(summary)
        with open(os.path.join(directory, f'{self.name}-changelog-entry.md'), 'w') as f:
            f.write(changelog)
        with open(os.path.join(directory, f'{self.name}-consolidated.json'), 'w') as f:
            json.dump(json.loads(self.get_consolidated()), f, sort_keys=True, indent=2)
        self.generate_xslx(os.path.join(directory, f'{self.name}-summary.xlsx'))

    def get_consolidated(self, as_api=True):
        """Get a consolidated package of the rules in a single file."""
        full_package = []
        for rule in self.rules:
            full_package.append(rule.get_payload() if as_api else rule.rule_format())

        return json.dumps(full_package, sort_keys=True)

    def save(self, verbose=True):
        """Save a package and all artifacts."""
        save_dir = os.path.join(RELEASE_DIR, self.name)
        rules_dir = os.path.join(save_dir, 'rules')
        extras_dir = os.path.join(save_dir, 'extras')

        # remove anything that existed before
        shutil.rmtree(save_dir, ignore_errors=True)
        os.makedirs(rules_dir, exist_ok=True)
        os.makedirs(extras_dir, exist_ok=True)

        for rule in self.rules:
            rule.save(new_path=os.path.join(rules_dir, os.path.basename(rule.path)))

        self._package_notice_file(rules_dir)
        self._package_index_file(rules_dir)

        if self.release:
            if self.registry_data:
                self._generate_registry_package(save_dir)

            self.save_release_files(extras_dir, self.changed_rule_ids, self.new_rules_ids, self.removed_rule_ids)

            # zip all rules only and place in extras
            shutil.make_archive(os.path.join(extras_dir, self.name), 'zip', root_dir=os.path.dirname(rules_dir),
                                base_dir=os.path.basename(rules_dir))

            # zip everything and place in release root
            shutil.make_archive(os.path.join(save_dir, '{}-all'.format(self.name)), 'zip',
                                root_dir=os.path.dirname(extras_dir), base_dir=os.path.basename(extras_dir))

        if verbose:
            click.echo('Package saved to: {}'.format(save_dir))

    def get_package_hash(self, as_api=True, verbose=True):
        """Get hash of package contents."""
        contents = base64.b64encode(self.get_consolidated(as_api=as_api).encode('utf-8'))
        sha256 = hashlib.sha256(contents).hexdigest()

        if verbose:
            click.echo('- sha256: {}'.format(sha256))

        return sha256

    @classmethod
    def from_config(cls, config: dict = None, update_version_lock: bool = False, verbose: bool = False) -> 'Package':
        """Load a rules package given a config."""
        all_rules = rule_loader.load_rules(verbose=False).values()
        config = config or {}
        exclude_fields = config.pop('exclude_fields', {})
        log_deprecated = config.pop('log_deprecated', False)
        rule_filter = config.pop('filter', {})

        deprecated_rules = [r for r in all_rules if r.metadata['maturity'] == 'deprecated'] if log_deprecated else []
        rules = list(filter(lambda rule: filter_rule(rule, rule_filter, exclude_fields), all_rules))

        if verbose:
            click.echo(f' - {len(all_rules) - len(rules)} rules excluded from package')

        update = config.pop('update', {})
        package = cls(rules, deprecated_rules=deprecated_rules, update_version_lock=update_version_lock, **config)

        # Allow for some fields to be overwritten
        if update.get('data', {}):
            for rule in package.rules:
                for sub_dict, values in update.items():
                    rule.contents[sub_dict].update(values)

        return package

    def generate_summary_and_changelog(self, changed_rule_ids, new_rule_ids, removed_rules):
        """Generate stats on package."""
        from string import ascii_lowercase, ascii_uppercase

        summary = {
            'changed': defaultdict(list),
            'added': defaultdict(list),
            'removed': defaultdict(list),
            'unchanged': defaultdict(list)
        }
        changelog = {
            'changed': defaultdict(list),
            'added': defaultdict(list),
            'removed': defaultdict(list),
            'unchanged': defaultdict(list)
        }

        # build an index map first
        longest_name = 0
        indexes = set()
        for rule in self.rules:
            longest_name = max(longest_name, len(rule.name))
            index_list = rule.contents.get('index')
            if index_list:
                indexes.update(index_list)

        letters = ascii_uppercase + ascii_lowercase
        index_map = {index: letters[i] for i, index in enumerate(sorted(indexes))}

        def get_summary_rule_info(r: Rule):
            rule_str = f'{r.name:<{longest_name}} (v:{r.contents.get("version")} t:{r.type}'
            rule_str += f'-{r.contents["language"]})' if r.contents.get('language') else ')'
            rule_str += f'(indexes:{"".join(index_map[i] for i in r.contents.get("index"))})' \
                if r.contents.get('index') else ''
            return rule_str

        def get_markdown_rule_info(r: Rule, sd):
            # lookup the rule in the GitHub tag v{major.minor.patch}
            rules_dir_link = f'https://github.com/elastic/detection-rules/tree/v{self.name}/rules/{sd}/'
            rule_type = r.contents['language'] if r.type in ('query', 'eql') else r.type
            return f'`{r.id}` **[{r.name}]({rules_dir_link + os.path.basename(r.path)})** (_{rule_type}_)'

        for rule in self.rules:
            sub_dir = os.path.basename(os.path.dirname(rule.path))

            if rule.id in changed_rule_ids:
                summary['changed'][sub_dir].append(get_summary_rule_info(rule))
                changelog['changed'][sub_dir].append(get_markdown_rule_info(rule, sub_dir))
            elif rule.id in new_rule_ids:
                summary['added'][sub_dir].append(get_summary_rule_info(rule))
                changelog['added'][sub_dir].append(get_markdown_rule_info(rule, sub_dir))
            else:
                summary['unchanged'][sub_dir].append(get_summary_rule_info(rule))
                changelog['unchanged'][sub_dir].append(get_markdown_rule_info(rule, sub_dir))

        for rule in self.deprecated_rules:
            sub_dir = os.path.basename(os.path.dirname(rule.path))

            if rule.id in removed_rules:
                summary['removed'][sub_dir].append(rule.name)
                changelog['removed'][sub_dir].append(rule.name)

        def format_summary_rule_str(rule_dict):
            str_fmt = ''
            for sd, rules in sorted(rule_dict.items(), key=lambda x: x[0]):
                str_fmt += f'\n{sd} ({len(rules)})\n'
                str_fmt += '\n'.join(' - ' + s for s in sorted(rules))
            return str_fmt or '\nNone'

        def format_changelog_rule_str(rule_dict):
            str_fmt = ''
            for sd, rules in sorted(rule_dict.items(), key=lambda x: x[0]):
                str_fmt += f'\n- **{sd}** ({len(rules)})\n'
                str_fmt += '\n'.join('   - ' + s for s in sorted(rules))
            return str_fmt or '\nNone'

        def rule_count(rule_dict):
            count = 0
            for _, rules in rule_dict.items():
                count += len(rules)
            return count

        today = str(datetime.date.today())
        summary_fmt = [f'{sf.capitalize()} ({rule_count(summary[sf])}): \n{format_summary_rule_str(summary[sf])}\n'
                       for sf in ('added', 'changed', 'removed', 'unchanged') if summary[sf]]

        change_fmt = [f'{sf.capitalize()} ({rule_count(changelog[sf])}): \n{format_changelog_rule_str(changelog[sf])}\n'
                      for sf in ('added', 'changed', 'removed') if changelog[sf]]

        summary_str = '\n'.join([
            f'Version {self.name}',
            f'Generated: {today}',
            f'Total Rules: {len(self.rules)}',
            f'Package Hash: {self.get_package_hash(verbose=False)}',
            '---',
            '(v: version, t: rule_type-language)',
            'Index Map:\n{}'.format("\n".join(f"  {v}: {k}" for k, v in index_map.items())),
            '',
            'Rules',
            *summary_fmt
        ])

        changelog_str = '\n'.join([
            f'# Version {self.name}',
            f'_Released {today}_',
            '',
            '### Rules',
            *change_fmt,
            '',
            '### CLI'
        ])

        return summary_str, changelog_str

    def generate_xslx(self, path):
        """Generate a detailed breakdown of a package in an excel file."""
        from .docs import PackageDocument

        doc = PackageDocument(path, self)
        doc.populate()
        doc.close()

    def _generate_registry_package(self, save_dir):
        """Generate the artifact for the oob package-storage."""
        from .schemas.registry_package import get_manifest

        assert self.registry_data

        registry_manifest = get_manifest(self.registry_data['format_version'])
        manifest = registry_manifest.Schema().load(self.registry_data)

        package_dir = Path(save_dir).joinpath(manifest.version)
        docs_dir = package_dir.joinpath('docs')
        rules_dir = package_dir.joinpath('kibana', 'rules')

        docs_dir.mkdir(parents=True)
        rules_dir.mkdir(parents=True)

        manifest_file = package_dir.joinpath('manifest.yml')
        readme_file = docs_dir.joinpath('README.md')

        manifest_file.write_text(json.dumps(manifest.dump(), indent=2, sort_keys=True))
        shutil.copyfile(CHANGELOG_FILE, str(rules_dir.joinpath('CHANGELOG.json')))

        for rule in self.rules:
            rule.save(new_path=str(rules_dir.joinpath(f'rule-{rule.id}.json')))

        readme_text = '# Detection rules\n'
        readme_text += '\n'
        readme_text += 'The detection rules package is a non-integration package to store all the rules and '
        readme_text += 'dependencies (e.g. ML jobs) for the detection engine within the Elastic Security application.\n'
        readme_text += '\n'

        readme_file.write_text(readme_text)

    def bump_versions(self, save_changes=False, current_versions=None):
        """Bump the versions of all production rules included in a release and optionally save changes."""
        return manage_versions(self.rules, current_versions=current_versions, save_changes=save_changes)
