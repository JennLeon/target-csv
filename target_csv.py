#!/usr/bin/env python3

import argparse
import io
import os
import sys
import json
import simplejson
import csv
import threading
import http.client
import urllib
from datetime import datetime
import collections
import pkg_resources
import pathlib

from jsonschema.validators import Draft4Validator
import singer

logger = singer.get_logger()


def emit_state(state):
    if state is not None:
        line = json.dumps(state)
        logger.debug('Emitting state {}'.format(line))
        sys.stdout.write("{}\n".format(line))
        sys.stdout.flush()


def flatten(d, parent_key='', sep='__'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten(v, new_key, sep=sep).items())
        else:
            items.append((new_key, str(v) if type(v) is list else v))
    return dict(items)


def persist_messages(delimiter, quotechar, messages, destination_path, fixed_headers, validate):
    state = None
    schemas = {}
    key_properties = {}
    headers = {}
    validators = {}
    logger.debug("The current delimiter {}".format(delimiter))
    now = datetime.now().strftime('%Y%m%dT%H%M%S')

    file_extension = '.csv'

    if delimiter == '\\t':
        delimiter = str('\t')
        file_extension = '.tsv'

    for message in messages:
        try:
            o = singer.parse_message(message).asdict()
        except json.decoder.JSONDecodeError:
            logger.error("Unable to parse:\n{}".format(message))
            raise
        message_type = o['type']
        if message_type == 'RECORD':
            if o['stream'] not in schemas:
                raise Exception("A record for stream {}"
                                "was encountered before a corresponding schema".format(o['stream']))
            if validate:
                validators[o['stream']].validate(o['record'])

            filename = o['stream'] + file_extension
            filename = os.path.expanduser(os.path.join(destination_path, filename))
            file_is_empty = (not os.path.isfile(filename)) or os.stat(filename).st_size == 0

            # flattened_record = flatten(o['record'])
            flattened_record = o['record']

            if fixed_headers is not None and o['stream'] in fixed_headers:
                if o['stream'] not in headers:
                    headers[o['stream']] = fixed_headers[o['stream']]
            else:
                if o['stream'] not in headers and not file_is_empty:
                    with open(filename, 'r') as csvfile:
                        reader = csv.reader(csvfile,
                                            delimiter=delimiter,
                                            quotechar=quotechar)
                        first_line = next(reader)
                        headers[o['stream']] = first_line if first_line else flattened_record.keys()
                else:
                    headers[o['stream']] = flattened_record.keys()

            with open(filename, 'a') as csvfile:
                writer = csv.DictWriter(csvfile,
                                        headers[o['stream']],
                                        extrasaction='ignore',
                                        delimiter=str(delimiter),
                                        quotechar=quotechar)
                if file_is_empty:
                    writer.writeheader()

                # We use simplejson to re-serialize the data to avoid formatting issues in the CSV
                r = simplejson.dumps(flattened_record)
                writer.writerow(simplejson.loads(r))

            job_metrics_file_path = os.path.expanduser(os.path.join(destination_path, "job_metrics.json"))

            if not os.path.isfile(job_metrics_file_path):
                pathlib.Path(job_metrics_file_path).touch()

            with open(job_metrics_file_path, 'r+') as job_metrics_file:
                content = dict()

                try:
                    content = json.loads(job_metrics_file.read())
                except Exception:
                    pass

                if not content.get('recordCount'):
                    content['recordCount'] = dict()

                content['recordCount'][o['stream']] = content['recordCount'].get(o['stream'], 0) + 1

                job_metrics_file.seek(0)
                job_metrics_file.write(json.dumps(content))

            state = None
        elif message_type == 'STATE':
            logger.debug('Setting state to {}'.format(o['value']))
            state = o['value']
        elif message_type == 'SCHEMA':
            stream = o['stream']
            schemas[stream] = o['schema']
            validators[stream] = Draft4Validator(o['schema'])
            key_properties[stream] = o['key_properties']
        else:
            logger.warning("Unknown message type {} in message {}"
                           .format(o['type'], o))

    return state


def send_usage_stats():
    try:
        version = pkg_resources.get_distribution('target-csv').version
        conn = http.client.HTTPConnection('collector.singer.io', timeout=10)
        conn.connect()
        params = {
            'e': 'se',
            'aid': 'singer',
            'se_ca': 'target-csv',
            'se_ac': 'open',
            'se_la': version,
        }
        conn.request('GET', '/i?' + urllib.parse.urlencode(params))
        response = conn.getresponse()
        conn.close()
    except:
        logger.debug('Collection request failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', help='Config file')
    args = parser.parse_args()

    if args.config:
        with open(args.config) as input_json:
            config = json.load(input_json)
    else:
        config = {}

    if not config.get('disable_collection', False):
        logger.info('Sending version information to singer.io. ' +
                    'To disable sending anonymous usage data, set ' +
                    'the config parameter "disable_collection" to true')
        threading.Thread(target=send_usage_stats).start()

    input_messages = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    state = persist_messages(delimiter=config.get('delimiter', ','),
                             quotechar=config.get('quotechar', '"'),
                             messages=input_messages,
                             destination_path=config.get('destination_path', ''),
                             fixed_headers=config.get('fixed_headers'),
                             validate=config.get('validate', True))

    emit_state(state)
    logger.debug("Exiting normally")


if __name__ == '__main__':
    main()
