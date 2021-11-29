import os
import time
import json
import unittest
import pytest
from unittest.mock import patch


import singer
import tap_kafka
from tap_kafka import common
from tap_kafka import sync
from tap_kafka.errors import (
    DiscoveryException,
    InvalidBookmarkException,
    InvalidTimestampException,
    InvalidAssignByKeyException,
    TimestampNotAvailableException
)
import confluent_kafka

from tests.unit.helper.kafka_consumer_mock import KafkaConsumerMock, KafkaConsumerMessageMock


def _get_resource_from_json(filename):
    with open('{}/resources/{}'.format(os.path.dirname(__file__), filename)) as json_resource:
        return json.load(json_resource)


def _message_to_singer_record(message):
    return {
        'message': message.get('value'),
        'message_timestamp': sync.get_timestamp_from_timestamp_tuple(message.get('timestamp')),
        'message_offset': message.get('offset'),
        'message_partition': message.get('partition')
    }


def _message_to_singer_state(message):
    return {
        'bookmarks': message
    }


def _delete_version_from_state_message(state):
    if 'bookmarks' in state:
        for key in state['bookmarks'].keys():
            if 'version' in state['bookmarks'][key]:
                del state['bookmarks'][key]['version']

    return state


def _dict_to_kafka_message(dict_m):
    return {
        **dict_m,
        **{
            'timestamp': tuple(dict_m.get('timestamp', []))
        }
    }


def _parse_stdout(stdout):
    stdout_messages = []

    # Process only json messages
    for s in stdout.split("\n"):
        try:
            stdout_messages.append(json.loads(s))
        except Exception as e:
            pass

    return stdout_messages


def _read_kafka_topic(config, state, kafka_messages):
    # Mock KafkaConsumer classes
    consumer = KafkaConsumerMock(kafka_messages)
    singer_messages = []

    # Store output singer messages in an array
    singer.write_message = lambda m: singer_messages.append(m.asdict())

    # Run sync_stream
    sync.read_kafka_topic(consumer, config, state)

    return singer_messages


def _assert_singer_messages_in_local_store_equal(local_store, topic, exp_records, exp_states):
    exp_singer_records = list(map(lambda x: _message_to_singer_record(x), exp_records))
    exp_singer_states = list(map(lambda x: _message_to_singer_state(x), exp_states))
    for msg in map(json.loads, local_store.messages):
        if msg['type'] == 'RECORD':
            assert msg['stream'] == topic
            record = msg['record']
            exp_singer_records.remove(record)

        if msg['type'] == 'STATE':
            state = _delete_version_from_state_message(msg['value'])
            exp_singer_states.remove(state)

    # All the fake kafka message that we generated in consumer have been observed as a part of the output
    assert len(exp_singer_records) == 0
    assert len(exp_singer_states) == 0


class TestSync(object):
    """
    Unit Tests
    """

    @classmethod
    def setup_class(self):
        self.config = {
            'topic': 'dummy_topic',
            'primary_keys': {},
            'max_runtime_ms': tap_kafka.DEFAULT_MAX_RUNTIME_MS,
            'consumer_timeout_ms': tap_kafka.DEFAULT_CONSUMER_TIMEOUT_MS,
            'commit_interval_ms': tap_kafka.DEFAULT_COMMIT_INTERVAL_MS
        }

    def test_generate_config_with_defaults(self):
        """Should generate config dictionary with every required and optional parameter with defaults"""
        minimal_config = {
            'topic': 'my_topic',
            'group_id': 'my_group_id',
            'bootstrap_servers': 'server1,server2,server3'
        }
        assert tap_kafka.generate_config(minimal_config) == {
            'topic': 'my_topic',
            'group_id': 'my_group_id',
            'bootstrap_servers': 'server1,server2,server3',
            'primary_keys': {},
            'max_runtime_ms': tap_kafka.DEFAULT_MAX_RUNTIME_MS,
            'commit_interval_ms': tap_kafka.DEFAULT_COMMIT_INTERVAL_MS,
            'consumer_timeout_ms': tap_kafka.DEFAULT_CONSUMER_TIMEOUT_MS,
            'session_timeout_ms': tap_kafka.DEFAULT_SESSION_TIMEOUT_MS,
            'heartbeat_interval_ms': tap_kafka.DEFAULT_HEARTBEAT_INTERVAL_MS,
            'max_poll_records': tap_kafka.DEFAULT_MAX_POLL_RECORDS,
            'max_poll_interval_ms': tap_kafka.DEFAULT_MAX_POLL_INTERVAL_MS
        }

    def test_generate_config_with_custom_parameters(self):
        """Should generate config dictionary with every required and optional parameter with custom values"""
        custom_config = {
            'topic': 'my_topic',
            'group_id': 'my_group_id',
            'bootstrap_servers': 'server1,server2,server3',
            'primary_keys': {
                'id': '$.jsonpath.to.primary_key'
            },
            'max_runtime_ms': 1111,
            'commit_interval_ms': 10000,
            'batch_size_rows': 2222,
            'batch_flush_interval_ms': 3333,
            'consumer_timeout_ms': 1111,
            'session_timeout_ms': 2222,
            'heartbeat_interval_ms': 3333,
            'max_poll_records': 4444,
            'max_poll_interval_ms': 5555,
            'encoding': 'iso-8859-1',
            'local_store_dir': '/tmp/local-store',
            'local_store_batch_size_rows': 500
        }
        assert tap_kafka.generate_config(custom_config) == {
            'topic': 'my_topic',
            'group_id': 'my_group_id',
            'bootstrap_servers': 'server1,server2,server3',
            'primary_keys': {
                'id': '$.jsonpath.to.primary_key'
            },
            'max_runtime_ms': 1111,
            'commit_interval_ms': 10000,
            'consumer_timeout_ms': 1111,
            'session_timeout_ms': 2222,
            'heartbeat_interval_ms': 3333,
            'max_poll_records': 4444,
            'max_poll_interval_ms': 5555
        }

    def test_generate_schema_with_no_pk(self):
        """Should not add extra column when no PK defined"""
        assert common.generate_schema([]) == \
            {
                "type": "object",
                "properties": {
                    "message_timestamp": {"type": ["integer", "string", "null"]},
                    "message_offset": {"type": ["integer", "null"]},
                    "message_partition": {"type": ["integer", "null"]},
                    "message": {"type": ["object", "array", "string", "null"]}
                }
            }

    def test_generate_schema_with_pk(self):
        """Should add one extra column if PK defined"""
        assert common.generate_schema(["id"]) == \
            {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "message_timestamp": {"type": ["integer", "string", "null"]},
                    "message_offset": {"type": ["integer", "null"]},
                    "message_partition": {"type": ["integer", "null"]},
                    "message": {"type": ["object", "array", "string", "null"]}
                }
            }

    def test_generate_schema_with_composite_pk(self):
        """Should add multiple extra columns if composite PK defined"""
        assert common.generate_schema(["id", "version"]) == \
            {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "version": {"type": ["string", "null"]},
                    "message_timestamp": {"type": ["integer", "string", "null"]},
                    "message_offset": {"type": ["integer", "null"]},
                    "message_partition": {"type": ["integer", "null"]},
                    "message": {"type": ["object", "array", "string", "null"]}
                }
            }

    def test_generate_catalog_with_no_pk(self):
        """table-key-properties should be empty list when no PK defined"""
        assert common.generate_catalog({"topic": "dummy_topic"}) == \
               [
                   {
                       "metadata": [
                           {
                               "breadcrumb": (),
                                "metadata": {"table-key-properties": []}
                           }
                       ],
                       "schema": {
                           "type": "object",
                           "properties": {
                                "message_timestamp": {"type": ["integer", "string", "null"]},
                                "message_offset": {"type": ["integer", "null"]},
                                "message_partition": {"type": ["integer", "null"]},
                                "message": {"type": ["object", "array", "string", "null"]}
                           }
                       },
                       "tap_stream_id": "dummy_topic"
                   }
               ]

    def test_generate_catalog_with_pk(self):
        """table-key-properties should be a list with single item when PK defined"""
        assert common.generate_catalog({"topic": "dummy_topic", "primary_keys": {"id": "^.dummyJson.id"}}) == \
               [
                   {
                       "metadata": [
                           {
                               "breadcrumb": (),
                                "metadata": {"table-key-properties": ["id"]}
                           }
                       ],
                       "schema": {
                           "type": "object",
                           "properties": {
                                "id": {"type": ["string", "null"]},
                                "message_timestamp": {"type": ["integer", "string", "null"]},
                                "message_offset": {"type": ["integer", "null"]},
                                "message_partition": {"type": ["integer", "null"]},
                                "message": {"type": ["object", "array", "string", "null"]}
                           }
                       },
                       "tap_stream_id": "dummy_topic"
                   }
               ]

    def test_generate_catalog_with_composite_pk(self):
        """table-key-properties should be a list with two items when composite PK defined"""
        assert common.generate_catalog({"topic": "dummy_topic", "primary_keys": {"id": "dummyJson.id", "version": "dummyJson.version"}}) == \
               [
                   {
                       "metadata": [
                           {
                               "breadcrumb": (),
                                "metadata": {"table-key-properties": ["id", "version"]}
                           }
                       ],
                       "schema": {
                           "type": "object",
                           "properties": {
                                "id": {"type": ["string", "null"]},
                                "version": {"type": ["string", "null"]},
                                "message_timestamp": {"type": ["integer", "string", "null"]},
                                "message_offset": {"type": ["integer", "null"]},
                                "message_partition": {"type": ["integer", "null"]},
                                "message": {"type": ["object", "array", "string", "null"]}
                           }
                       },
                       "tap_stream_id": "dummy_topic"
                   }
               ]

    def test_get_timestamp_from_timestamp_tuple__invalid_tuple(self):
        """Argument needs to be a tuple"""
        # Passing number should raise exception
        with pytest.raises(InvalidTimestampException):
            assert sync.get_timestamp_from_timestamp_tuple(0)

        # String should raise exception
        with pytest.raises(InvalidTimestampException):
            assert sync.get_timestamp_from_timestamp_tuple("not-a-tuple")

        # List should raise exception
        with pytest.raises(InvalidTimestampException):
            assert sync.get_timestamp_from_timestamp_tuple([])

        # Valid timestamp but as list should raise exception
        with pytest.raises(InvalidTimestampException):
            assert sync.get_timestamp_from_timestamp_tuple([confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789])

        # Dict should raise exception
        with pytest.raises(InvalidTimestampException):
            assert sync.get_timestamp_from_timestamp_tuple({})

        # Empty tuple should raise exception
        with pytest.raises(InvalidTimestampException):
            sync.get_timestamp_from_timestamp_tuple(())

        # Tuple with one element should raise exception
        with pytest.raises(InvalidTimestampException):
            sync.get_timestamp_from_timestamp_tuple(tuple([confluent_kafka.TIMESTAMP_CREATE_TIME]))

        # Zero timestamp should raise exception
        with pytest.raises(InvalidTimestampException):
            sync.get_timestamp_from_timestamp_tuple((confluent_kafka.TIMESTAMP_CREATE_TIME, 0))

        # Negative timestamp should raise exception
        with pytest.raises(InvalidTimestampException):
            sync.get_timestamp_from_timestamp_tuple((confluent_kafka.TIMESTAMP_CREATE_TIME, -9876))

    def test_get_timestamp_from_timestamp_tuple__valid_tuple(self):
        """Argument needs to be a tuple"""
        assert sync.get_timestamp_from_timestamp_tuple((confluent_kafka.TIMESTAMP_CREATE_TIME, 9876)) == 9876

    def test_search_in_list_of_dict_by_key_value(self):
        """Search in list of dictionaries by key and value"""
        # No match should return -1
        list_of_dict = [{}, {'search_key': 'search_val_X'}]
        assert sync.search_in_list_of_dict_by_key_value(list_of_dict, 'search_key', 'search_val') == -1

        # Should return second position (1)
        list_of_dict = [{}, {'search_key': 'search_val'}]
        assert sync.search_in_list_of_dict_by_key_value(list_of_dict, 'search_key', 'search_val') == 1

        # Multiple match should return the first match postiong (0)
        list_of_dict = [{'search_key': 'search_val'}, {'search_key': 'search_val'}]
        assert sync.search_in_list_of_dict_by_key_value(list_of_dict, 'search_key', 'search_val') == 0

    def test_send_activate_version_message(self):
        """ACTIVATE_VERSION message should be generated from bookmark"""
        singer_messages = []

        # Store output singer messages in an array
        singer.write_message = lambda m: singer_messages.append(m.asdict())

        # If no bookmarked version then it should generate a timestamp
        state = _get_resource_from_json('state-with-bookmark-with-version.json')
        sync.send_activate_version_message(state, 'dummy_topic')
        assert singer_messages == [
            {
                'stream': 'dummy_topic',
                'type': 'ACTIVATE_VERSION',
                'version': 9999
            }
        ]

        # If no bookmarked version then it should generate a timestamp
        singer_messages = []
        now = int(time.time() * 1000)
        state = _get_resource_from_json('state-with-bookmark.json')
        sync.send_activate_version_message(state, 'dummy_topic')
        assert singer_messages[0]['version'] >= now
        assert singer_messages == [
            {
                'stream': 'dummy_topic',
                'type': 'ACTIVATE_VERSION',
                'version': singer_messages[0]['version']
            }
        ]

    def test_send_schema_message(self):
        """SCHEME message should be generated from catalog"""
        singer_messages = []

        # Store output singer messages in an array
        singer.write_message = lambda m: singer_messages.append(m.asdict())

        catalog = _get_resource_from_json('catalog.json')
        streams = catalog.get('streams', [])
        topic_pos = sync.search_in_list_of_dict_by_key_value(streams, 'tap_stream_id', 'dummy_topic')
        stream = streams[topic_pos]

        sync.send_schema_message(stream)
        assert singer_messages == [
            {
                'type': 'SCHEMA',
                'stream': 'dummy_topic',
                'schema': {
                    'type': 'object',
                    'properties': {
                        'id': {'type': ['string', 'null']},
                        'message_partition': {'type': ['integer', 'null']},
                        'message_offset': {'type': ['integer', 'null']},
                        'message_timestamp': {'type': ['integer', 'string', 'null']},
                        'message': {'type': ['object', 'array', 'string', 'null']}
                    }
                },
                'key_properties': ['id']
            }
        ]

    def test_update_bookmark__on_empty_state(self):
        """Updating empty state should generate a new bookmark"""
        topic = 'test-topic'
        input_state = {}
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789),
                                           offset=1234,
                                           partition=0)
        assert sync.update_bookmark(input_state, topic, message) == \
            {'bookmarks': {'test-topic': {'partition_0': {'partition': 0, 'offset': 1234, 'timestamp': 123456789}}}}

    def test_update_bookmark__update_stream(self):
        """Updating existing bookmark in state should update at every property"""
        topic = 'test-topic-updated'
        input_state = {'bookmarks': {'test-topic-updated': {'partition_0': {'partition': 0,
                                                                            'offset': 1234,
                                                                            'timestamp': 111}}}}
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 999999999),
                                           offset=999,
                                           partition=0)

        assert sync.update_bookmark(input_state, topic, message) == \
            {'bookmarks': {'test-topic-updated': {'partition_0': {'partition': 0,
                                                                  'offset': 999,
                                                                  'timestamp': 999999999}}}}

    def test_update_bookmark__add_new_partition(self):
        """Updating existing bookmark in state should update at every property"""
        topic = 'test-topic-updated'
        input_state = {'bookmarks': {'test-topic-updated': {'partition_0': {'partition': 0,
                                                                            'offset': 1234,
                                                                            'timestamp': 111}}}}
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789),
                                           offset=111,
                                           partition=1)

        assert sync.update_bookmark(input_state, topic, message) == \
            {'bookmarks': {'test-topic-updated': {'partition_0': {'partition': 0,
                                                                  'offset': 1234,
                                                                  'timestamp': 111},
                                                  'partition_1': {'partition': 1,
                                                                  'offset': 111,
                                                                  'timestamp': 123456789}}}}

    def test_update_bookmark__update_partition(self):
        """Updating existing bookmark in state should update at every property"""
        topic = 'test-topic-updated'
        input_state = {'bookmarks': {'test-topic-updated': {'partition_0': {'partition': 0,
                                                                            'offset': 1234,
                                                                            'timestamp': 111},
                                                            'partition_1': {'partition': 0,
                                                                            'offset': 1234,
                                                                            'timestamp': 111}}}}
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789),
                                           offset=111,
                                           partition=1)

        assert sync.update_bookmark(input_state, topic, message) == \
            {'bookmarks': {'test-topic-updated': {'partition_0': {'partition': 0,
                                                                  'offset': 1234,
                                                                  'timestamp': 111},
                                                  'partition_1': {'partition': 1,
                                                                  'offset': 111,
                                                                  'timestamp': 123456789}}}}

    def test_update_bookmark__add_new_stream(self):
        """Updating a not existing stream id should be appended to the bookmarks dictionary"""
        input_state = {'bookmarks': {'test-topic-0': {'partition_0': {'partition': 0,
                                                                      'offset': 1234,
                                                                      'timestamp': 111},
                                                      'partition_1': {'partition': 1,
                                                                      'offset': 111,
                                                                      'timestamp': 1234}}}}
        message = KafkaConsumerMessageMock(topic='test-topic-1',
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789),
                                           offset=111,
                                           partition=0)

        assert sync.update_bookmark(input_state, 'test-topic-1', message) == \
            {'bookmarks': {'test-topic-0': {'partition_0': {'partition': 0,
                                                            'offset': 1234,
                                                            'timestamp': 111},
                                            'partition_1': {'partition': 1,
                                                            'offset': 111,
                                                            'timestamp': 1234}},
                           'test-topic-1': {'partition_0': {'partition': 0,
                                                            'offset': 111,
                                                            'timestamp': 123456789}}}}

    def test_update_bookmark__not_integer(self):
        """Timestamp in the bookmark should be auto-converted to int whenever it's possible"""
        topic = 'test-topic-updated'
        input_state = {'bookmarks': {topic: {'partition_0': {'partition': 0,
                                                             'offset': 1234,
                                                             'timestamp': 111}}}}

        # Timestamp should be converted from string to int
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, "123456789"),
                                           offset=111,
                                           partition=0)
        assert sync.update_bookmark(input_state, topic, message) == \
            {'bookmarks': {'test-topic-updated': {'partition_0': {'partition': 0,
                                                                  'offset': 111,
                                                                  'timestamp': 123456789}}}}

        # Timestamp that cannot be converted to int should raise exception
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, "this-is-not-numeric"),
                                           offset=111,
                                           partition=0)
        with pytest.raises(InvalidTimestampException):
            assert sync.update_bookmark(input_state, topic, message)

    @patch('tap_kafka.sync.commit_consumer_to_bookmarked_state')
    def test_consuming_records_with_no_state(self, commit_consumer_to_bookmarked_state):
        """Every consumed kafka message should generate a valid singer RECORD and a STATE messages at the end

        - Kafka commit should be called at least once at the end
        - STATE should return the last consumed message offset and timestamp per partition"""
        # Set test inputs
        state = {}
        messages = _get_resource_from_json('kafka-messages-from-multiple-partitions.json')
        kafka_messages = list(map(_dict_to_kafka_message, messages))

        # Run test
        singer_messages = _read_kafka_topic(self.config, state, kafka_messages)
        assert singer_messages == [
            {
                'type': 'ACTIVATE_VERSION',
                'stream': 'dummy_topic',
                'version': singer_messages[0]['version']
            },
            {
                'type': 'RECORD',
                'stream': 'dummy_topic',
                'record': {
                    'message': {'result': 'SUCCESS', 'details': {'id': '1001', 'type': 'TYPE_1', 'profileId': 1234}},
                    'message_partition': 1,
                    'message_offset': 1,
                    'message_timestamp': 1575895711187
                },
                'time_extracted': singer_messages[1]['time_extracted']
            },
            {
                'type': 'RECORD',
                'stream': 'dummy_topic',
                'record': {
                    'message': {'result': 'SUCCESS', 'details': {'id': '1002', 'type': 'TYPE_2', 'profileId': 1234}},
                    'message_partition': 2,
                    'message_offset': 2,
                    'message_timestamp': 1575895711188
                },
                'time_extracted': singer_messages[2]['time_extracted']
            },
            {
                'type': 'RECORD',
                'stream': 'dummy_topic',
                'record': {
                    'message': {'result': 'SUCCESS', 'details': {'id': '1003', 'type': 'TYPE_3', 'profileId': 1234}},
                    'message_partition': 2,
                    'message_offset': 3,
                    'message_timestamp': 1575895711189
                },
                'time_extracted': singer_messages[3]['time_extracted']
            },
            {
                'type': 'STATE',
                'value': {
                    'bookmarks': {
                        'dummy_topic': {'partition_1': {'partition': 1,
                                                        'offset': 1,
                                                        'timestamp': 1575895711187},
                                        'partition_2': {'partition': 2,
                                                        'offset': 3,
                                                        'timestamp': 1575895711189}}
                    }
                }
            }
        ]

        # Kafka commit should be called at least once
        assert commit_consumer_to_bookmarked_state.call_count > 0

    @patch('tap_kafka.sync.commit_consumer_to_bookmarked_state')
    def test_consuming_records_with_state(self, commit_consumer_to_bookmarked_state):
        """Every consumed kafka message should generate a valid singer RECORD and a STATE messages at the end

        - Kafka commit should be called at least once at the end
        - STATE should return the last consumed message offset and timestamp per partition"""
        # Set test inputs
        state = _get_resource_from_json('state-with-bookmark.json')
        messages = _get_resource_from_json('kafka-messages-from-multiple-partitions.json')
        kafka_messages = list(map(_dict_to_kafka_message, messages))

        # Run test
        consumed_messages = _read_kafka_topic(self.config, state, kafka_messages)
        assert consumed_messages == [
            {
                'type': 'ACTIVATE_VERSION',
                'stream': 'dummy_topic',
                'version': consumed_messages[0]['version']
            },
            {
                'type': 'RECORD',
                'stream': 'dummy_topic',
                'record': {
                    'message': {'result': 'SUCCESS', 'details': {'id': '1001', 'type': 'TYPE_1', 'profileId': 1234}},
                    'message_partition': 1,
                    'message_offset': 1,
                    'message_timestamp': 1575895711187
                },
                'time_extracted': consumed_messages[1]['time_extracted']
            },
            {
                'type': 'RECORD',
                'stream': 'dummy_topic',
                'record': {
                    'message': {'result': 'SUCCESS', 'details': {'id': '1002', 'type': 'TYPE_2', 'profileId': 1234}},
                    'message_partition': 2,
                    'message_offset': 2,
                    'message_timestamp': 1575895711188
                },
                'time_extracted': consumed_messages[2]['time_extracted']
            },
            {
                'type': 'RECORD',
                'stream': 'dummy_topic',
                'record': {
                    'message': {'result': 'SUCCESS', 'details': {'id': '1003', 'type': 'TYPE_3', 'profileId': 1234}},
                    'message_partition': 2,
                    'message_offset': 3,
                    'message_timestamp': 1575895711189
                },
                'time_extracted': consumed_messages[3]['time_extracted']
            },
            {
                'type': 'STATE',
                'value': {
                    'bookmarks': {
                        'dummy_topic': {'partition_1': {'partition': 1,
                                                        'offset': 1,
                                                        'timestamp': 1575895711187},
                                        'partition_2': {'partition': 2,
                                                        'offset': 3,
                                                        'timestamp': 1575895711189}}
                    }
                }
            }
        ]

        # Kafka commit should be called at least once
        assert commit_consumer_to_bookmarked_state.call_count > 0

    def test_kafka_message_to_singer_record(self):
        """Validate if kafka messages converted to singer messages correctly"""
        topic = 'test-topic'

        # Converting without primary key
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789),
                                           offset=1234,
                                           partition=0)
        primary_keys = {}
        assert sync.kafka_message_to_singer_record(message, primary_keys) == {
            'message': {'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
            'message_timestamp': 123456789,
            'message_offset': 1234,
            'message_partition': 0
        }

        # Converting with primary key
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789),
                                           offset=1234,
                                           partition=0)
        primary_keys = {'id': '/id'}
        assert sync.kafka_message_to_singer_record(message, primary_keys) == {
            'message': {'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
            'id': 1,
            'message_timestamp': 123456789,
            'message_offset': 1234,
            'message_partition': 0
        }

        # Converting with nested and multiple primary keys
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789),
                                           offset=1234,
                                           partition=0)
        primary_keys = {'id': '/id', 'y': '/data/y'}
        assert sync.kafka_message_to_singer_record(message, primary_keys) == {
            'message': {'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
            'id': 1,
            'y': 'value-y',
            'message_timestamp': 123456789,
            'message_offset': 1234,
            'message_partition': 0
        }

        # Converting with not existing primary keys
        message = KafkaConsumerMessageMock(topic=topic,
                                           value={'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
                                           timestamp=(confluent_kafka.TIMESTAMP_CREATE_TIME, 123456789),
                                           offset=1234,
                                           partition=0)
        primary_keys = {'id': '/id', 'not-existing-key': '/path/not/exists'}
        assert sync.kafka_message_to_singer_record(message, primary_keys) == {
            'message': {'id': 1, 'data': {'x': 'value-x', 'y': 'value-y'}},
            'id': 1,
            'message_timestamp': 123456789,
            'message_offset': 1234,
            'message_partition': 0
        }

    def test_commit_consumer_to_bookmarked_state(self):
        """Commit should commit every partition in the bookmark state"""
        topic = 'test_topic'

        # If one partition bookmarked then need to commit one offset
        state = {'bookmarks': {topic: {'partition_0': {'partition': 0,
                                                       'offset': 1234,
                                                       'timestamp': 123456789}}}}
        consumer = KafkaConsumerMock(fake_messages=[])
        sync.commit_consumer_to_bookmarked_state(consumer, topic, state)
        assert consumer.committed_offsets == [
            confluent_kafka.TopicPartition(topic=topic, partition=0, offset=1234)
        ]

        # If multiple partitions bookmarked then need to commit every offset
        state = {'bookmarks': {topic: {'partition_0': {'partition': 0,
                                                       'offset': 1234,
                                                       'timestamp': 123456789},
                                       'partition_1': {'partition': 1,
                                                       'offset': 2345,
                                                       'timestamp': 123456789},
                                       'partition_2': {'partition': 2,
                                                       'offset': 3456,
                                                       'timestamp': 123456789}
                                       }}}
        consumer = KafkaConsumerMock(fake_messages=[])
        sync.commit_consumer_to_bookmarked_state(consumer, topic, state)
        assert consumer.committed_offsets == [
            confluent_kafka.TopicPartition(topic=topic, partition=0, offset=1234),
            confluent_kafka.TopicPartition(topic=topic, partition=1, offset=2345),
            confluent_kafka.TopicPartition(topic=topic, partition=2, offset=3456)
        ]

    def test_bookmarked_partition_to_next_position(self):
        """Transform a bookmarked partition to a kafka TopicPartition object"""
        topic = 'test_topic'
        partition_bookmark = {'partition': 0, 'offset': 1234, 'timestamp': 1638132327000}

        # By default TopicPartition offset needs to be bookmarked timestamp and not offset
        topic_partition = sync.bookmarked_partition_to_next_position(topic, partition_bookmark)
        assert topic_partition.topic == topic
        assert topic_partition.partition == 0
        assert topic_partition.offset == 1638132327000

        # Assigning by timestamp explicitly should behave the same as not providing the assing_by parameter
        topic_partition = sync.bookmarked_partition_to_next_position(topic, partition_bookmark, assign_by='timestamp')
        assert topic_partition.topic == topic
        assert topic_partition.partition == 0
        assert topic_partition.offset == 1638132327000

        # Assigning by offset should increase the offset by 1, pointing to the next not consumed offset
        topic_partition = sync.bookmarked_partition_to_next_position(topic, partition_bookmark, assign_by='offset')
        assert topic_partition.topic == topic
        assert topic_partition.partition == 0
        assert topic_partition.offset == 1235   # Bookmarked offset +1

    def test_bookmarked_partition_to_next_position__invalid_options(self):
        """Transform a bookmarked partition to a kafka TopicPartition object"""
        topic = 'test_topic'

        # Empty bookmark should raise exception
        partition_bookmark = {}
        with pytest.raises(InvalidBookmarkException):
            sync.bookmarked_partition_to_next_position(topic, partition_bookmark)

        # Partially provided bookmark - no partition
        partition_bookmark = {'offset': 1234, 'timestamp': 1638132327000}
        with pytest.raises(InvalidBookmarkException):
            sync.bookmarked_partition_to_next_position(topic, partition_bookmark)

        # Should raise an exception if partition is not int
        partition_bookmark = {'partition': '0', 'offset': 1234, 'timestamp': 1638132327000}
        with pytest.raises(InvalidBookmarkException):
            sync.bookmarked_partition_to_next_position(topic, partition_bookmark)

        # Should raise an exception if timestamp is not int
        partition_bookmark = {'partition': 0, 'offset': 1234, 'timestamp': '1638132327000'}
        with pytest.raises(InvalidBookmarkException):
            sync.bookmarked_partition_to_next_position(topic, partition_bookmark)

        # Should raise an exception if offset is not int
        partition_bookmark = {'partition': 0, 'offset': '1234', 'timestamp': 1638132327000}
        with pytest.raises(InvalidBookmarkException):
            sync.bookmarked_partition_to_next_position(topic, partition_bookmark, assign_by='offset')

        # Assigning by invalid option
        partition_bookmark = {'partition': 0, 'offset': 1234, 'timestamp': 1638132327000}
        with pytest.raises(InvalidAssignByKeyException):
            sync.bookmarked_partition_to_next_position(topic, partition_bookmark, assign_by='invalid-option')

    def test_do_disovery_failure(self):
        """Validate if kafka messages converted to singer messages correctly"""
        minimal_config = {
            'topic': 'not_existing_topic',
            'group_id': 'my_group_id',
            'bootstrap_servers': 'not-existing-server1,not-existing-server2',
            'session_timeout_ms': 1000,
        }
        config = tap_kafka.generate_config(minimal_config)

        with pytest.raises(DiscoveryException):
            tap_kafka.do_discovery(config)

    def test_get_timestamp_from_timestamp_tuple(self):
        """Validate if the actual timestamp can be extracted from a kafka timestamp"""
        # Timestamps as tuples
        assert sync.get_timestamp_from_timestamp_tuple((confluent_kafka.TIMESTAMP_CREATE_TIME, 1234)) == 1234
        assert sync.get_timestamp_from_timestamp_tuple((confluent_kafka.TIMESTAMP_LOG_APPEND_TIME, 1234)) == 1234

        # Timestamp not available
        with pytest.raises(TimestampNotAvailableException):
            sync.get_timestamp_from_timestamp_tuple((confluent_kafka.TIMESTAMP_NOT_AVAILABLE, 1234))

        # Invalid timestamp type
        with pytest.raises(InvalidTimestampException):
            sync.get_timestamp_from_timestamp_tuple(([confluent_kafka.TIMESTAMP_CREATE_TIME, 1234], 1234))

        # Invalid timestamp type
        with pytest.raises(InvalidTimestampException):
            sync.get_timestamp_from_timestamp_tuple((9999, 1234))

        # Invalid timestamp type
        with pytest.raises(InvalidTimestampException):
            sync.get_timestamp_from_timestamp_tuple("not_a_tuple_or_list")


if __name__ == '__main__':
    unittest.main()
