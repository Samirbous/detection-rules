# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License;
# you may not use this file except in compliance with the Elastic License.

"""Elasticsearch cli commands."""
import json
import os
import time

import click
import elasticsearch
from elasticsearch import Elasticsearch
from eql.utils import load_dump


from .main import root
from .misc import add_params, client_error, elasticsearch_options
from .utils import format_command_options, normalize_timing_and_sort, unix_time_to_formatted, get_path
from .rule import Rule
from .rule_loader import get_rule, rta_mappings

COLLECTION_DIR = get_path('collections')


def get_authed_es_client(cloud_id=None, elasticsearch_url=None, es_user=None, es_password=None, ctx=None, **kwargs):
    """Get an authenticated elasticsearch client."""
    if not (cloud_id or elasticsearch_url):
        client_error("Missing required --cloud-id or --elasticsearch-url")

    # don't prompt for these until there's a cloud id or elasticsearch URL
    es_user = es_user or click.prompt("es_user")
    es_password = es_password or click.prompt("es_password", hide_input=True)
    hosts = [elasticsearch_url] if elasticsearch_url else None

    try:
        client = Elasticsearch(hosts=hosts, cloud_id=cloud_id, http_auth=(es_user, es_password), **kwargs)
        # force login to test auth
        client.info()
        return client
    except elasticsearch.AuthenticationException as e:
        error_msg = f'Failed authentication for {elasticsearch_url or cloud_id}'
        client_error(error_msg, e, ctx=ctx, err=True)


def add_range_to_dsl(dsl_filter, start_time, end_time='now'):
    dsl_filter.append(
        {"range": {"@timestamp": {"gt": start_time, "lte": end_time, "format": "strict_date_optional_time"}}}
    )


class Events(object):
    """Events collected from Elasticsearch."""

    def __init__(self, agent_hostname, events):
        self.agent_hostname = agent_hostname
        self.events = self._normalize_event_timing(events)

    @staticmethod
    def _normalize_event_timing(events):
        """Normalize event timestamps and sort."""
        for agent_type, _events in events.items():
            events[agent_type] = normalize_timing_and_sort(_events)

        return events

    def _get_dump_dir(self, rta_name=None):
        """Prepare and get the dump path."""
        if rta_name:
            dump_dir = get_path('unit_tests', 'data', 'true_positives', rta_name)
            os.makedirs(dump_dir, exist_ok=True)
            return dump_dir
        else:
            time_str = time.strftime('%Y%m%dT%H%M%SL')
            dump_dir = os.path.join(COLLECTION_DIR, self.agent_hostname, time_str)
            os.makedirs(dump_dir, exist_ok=True)
            return dump_dir

    def evaluate_against_rule_and_update_mapping(self, rule_id, rta_name, verbose=True):
        """Evaluate a rule against collected events and update mapping."""
        from .utils import combine_sources, evaluate

        rule = get_rule(rule_id, verbose=False)
        merged_events = combine_sources(*self.events.values())
        filtered = evaluate(rule, merged_events)

        if filtered:
            sources = [e['agent']['type'] for e in filtered]
            mapping_update = rta_mappings.add_rule_to_mapping_file(rule, len(filtered), rta_name, *sources)

            if verbose:
                click.echo('Updated rule-mapping file with: \n{}'.format(json.dumps(mapping_update, indent=2)))
        else:
            if verbose:
                click.echo('No updates to rule-mapping file; No matching results')

    def echo_events(self, pager=False, pretty=True):
        """Print events to stdout."""
        echo_fn = click.echo_via_pager if pager else click.echo
        echo_fn(json.dumps(self.events, indent=2 if pretty else None, sort_keys=True))

    def save(self, rta_name=None, dump_dir=None):
        """Save collected events."""
        assert self.events, 'Nothing to save. Run Collector.run() method first'

        dump_dir = dump_dir or self._get_dump_dir(rta_name)

        for source, events in self.events.items():
            path = os.path.join(dump_dir, source + '.jsonl')
            with open(path, 'w') as f:
                f.writelines([json.dumps(e, sort_keys=True) + '\n' for e in events])
                click.echo('{} events saved to: {}'.format(len(events), path))


class CollectEvents(object):
    """Event collector for elastic stack."""

    def __init__(self, client, max_events=3000):
        self.client = client
        self.MAX_EVENTS = max_events

    def _build_timestamp_map(self, index_str):
        """Build a mapping of indexes to timestamp data formats."""
        mappings = self.client.indices.get_mapping(index=index_str)
        timestamp_map = {n: m['mappings'].get('properties', {}).get('@timestamp', {}) for n, m in mappings.items()}
        return timestamp_map

    def _get_current_time(self, agent_hostname, index_str):
        """Get timestamp of most recent event."""
        # https://www.elastic.co/guide/en/elasticsearch/reference/current/mapping-date-format.html
        timestamp_map = self._build_timestamp_map(index_str)

        last_event = self._search_window(agent_hostname, index_str, start_time='now-1m', size=1, sort='@timestamp:desc')
        last_event = last_event['hits']['hits'][0]

        index = last_event['_index']
        timestamp = last_event['_source']['@timestamp']
        event_date_format = timestamp_map[index].get('format', '').split('||')

        # there are many native supported date formats and even custom data formats, but most, including beats use the
        # default `strict_date_optional_time`. It would be difficult to try to account for all possible formats, so this
        # will work on the default and unix time.
        if set(event_date_format) & {'epoch_millis', 'epoch_second'}:
            timestamp = unix_time_to_formatted(timestamp)

        return timestamp

    def _search_window(self, agent_hostname, index_str, start_time, end_time='now', size=None, sort='@timestamp:asc',
                       **match):
        """Collect all events within a time window and parse by source."""
        match = match.copy()
        match.update({"agent.hostname": agent_hostname})
        body = {"query": {"bool": {"filter": [
            {"match": {"agent.hostname": agent_hostname}},
            {"range": {"@timestamp": {"gt": start_time, "lte": end_time, "format": "strict_date_optional_time"}}}]
        }}}

        if match:
            body['query']['bool']['filter'].extend([{'match': {k: v}} for k, v in match.items()])

        return self.client.search(index=index_str, body=body, size=size or self.MAX_EVENTS, sort=sort)

    @staticmethod
    def _group_events_by_type(events):
        """Group events by agent.type."""
        event_by_type = {}

        for event in events['hits']['hits']:
            event_by_type.setdefault(event['_source']['agent']['type'], []).append(event['_source'])

        return event_by_type

    def run(self, agent_hostname, indexes, verbose=True, **match):
        """Collect the events."""
        index_str = ','.join(indexes)
        start_time = self._get_current_time(agent_hostname, index_str)

        if verbose:
            click.echo('Setting start of event capture to: {}'.format(click.style(start_time, fg='yellow')))

        click.pause('Press any key once detonation is complete ...')
        time.sleep(5)
        events = self._group_events_by_type(self._search_window(agent_hostname, index_str, start_time, **match))

        return Events(agent_hostname, events)


@root.command('normalize-data')
@click.argument('events-file', type=click.File('r'))
def normalize_data(events_file):
    """Normalize Elasticsearch data timestamps and sort."""
    file_name = os.path.splitext(os.path.basename(events_file.name))[0]
    events = Events('_', {file_name: [json.loads(e) for e in events_file.readlines()]})
    events.save(dump_dir=os.path.dirname(events_file.name))


@root.group('es')
@add_params(*elasticsearch_options)
@click.pass_context
def es_group(ctx: click.Context, **kwargs):
    """Commands for integrating with Elasticsearch."""
    ctx.ensure_object(dict)

    # only initialize an es client if the subcommand is invoked without help (hacky)
    if click.get_os_args()[-1] in ctx.help_option_names:
        click.echo('Elasticsearch client:')
        click.echo(format_command_options(ctx))

    else:
        ctx.obj['es'] = get_authed_es_client(ctx=ctx, **kwargs)


@es_group.command('collect-events')
@click.argument('agent-hostname')
@click.option('--index', '-i', multiple=True, help='Index(es) to search against (default: all indexes)')
@click.option('--agent-type', '-a', help='Restrict results to a source type (agent.type) ex: auditbeat')
@click.option('--rta-name', '-r', help='Name of RTA in order to save events directly to unit tests data directory')
@click.option('--rule-id', help='Updates rule mapping in rule-mapping.yml file (requires --rta-name)')
@click.option('--view-events', is_flag=True, help='Print events after saving')
@click.pass_context
def collect_events(ctx, agent_hostname, index, agent_type, rta_name, rule_id, view_events):
    """Collect events from Elasticsearch."""
    match = {'agent.type': agent_type} if agent_type else {}
    client = ctx.obj['es']

    try:
        collector = CollectEvents(client)
        events = collector.run(agent_hostname, index, **match)
        events.save(rta_name)

        if rta_name and rule_id:
            events.evaluate_against_rule_and_update_mapping(rule_id, rta_name)

        if view_events and events.events:
            events.echo_events(pager=True)

        return events
    except AssertionError as e:
        error_msg = 'No events collected! Verify events are streaming and that the agent-hostname is correct'
        client_error(error_msg, e, ctx=ctx)


@es_group.command('event-search')
@click.argument('query')
@click.option('--index', '-i', multiple=True, required=True, help='Index(es) to search against ("*": for all indexes)')
@click.option('--eql/--lucene', '-e/-l', 'language', default=None, help='Query language used (default: kql)')
@click.option('--count', '-c', is_flag=True, help='Return count of results only')
@click.option('--verbose', '-v', is_flag=True)
@click.pass_context
def event_search(ctx: click.Context, query, index, language, count, verbose):
    """Search using a query against an Elasticsearch instance."""
    import kql

    client = ctx.obj['es']

    language_used = "kql" if language is None else "eql" if language is True else "lucene"

    index_str = ','.join(index)
    formatted_query = {'query': kql.to_dsl(query)} if language_used == 'kql' else \
        {'query': query} if language_used == 'eql' else None

    if verbose:
        click.echo(f'{language_used}: {formatted_query or query}')

    if count:
        count = client.count(body=formatted_query, index=index_str)
        click.echo(f'Total results: {count["count"]}')
        return count
    else:
        if language_used != 'eql':
            results = client.search(body=formatted_query, q=query if language_used == 'lucene' else None,
                                    index=index_str)
        else:
            results = client.eql.search(body=formatted_query, index=index_str)

        click.echo_via_pager(json.dumps(results['hits']['events'], indent=2, sort_keys=True))
        return results


@es_group.command('rule-event-search')
@click.argument('rule-file', type=click.Path(dir_okay=False), required=False)
@click.option('--rule-id', '-id')
@click.option('--count', '-c', is_flag=True, help='Return count of results only')
@click.option('--verbose', '-v', is_flag=True)
@click.pass_context
def rule_event_search(ctx, rule_file, rule_id, count, verbose):
    """Search using a rule file against an Elasticsearch instance."""
    rule = None

    if rule_id:
        rule = get_rule(rule_id, verbose=False)
    elif rule_file:
        rule = Rule(rule_file, load_dump(rule_file))
    else:
        client_error('Must specify a rule file or rule ID')

    if rule.contents.get('query') and rule.contents.get('language'):
        if verbose:
            click.echo(f'Searching rule: {rule.name}')

        rule_lang = rule.contents.get('language')
        language = None if rule_lang == 'kuery' else True if rule_lang == 'eql' else "lucene"
        ctx.invoke(event_search, query=rule.query, index=rule.contents.get('index', "*"), language=language,
                   count=count, verbose=verbose)
    else:
        client_error('Rule is not a query rule!')
