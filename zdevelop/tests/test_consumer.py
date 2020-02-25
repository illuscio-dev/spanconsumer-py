import pytest
import grahamcracker
import aio_pika
import asyncio
import threading
import uuid
import time
import csv
import io
from collections import Counter
from typing import List, Dict, Any

from dataclasses import dataclass
from unittest import mock

from spantools import MimeType

from spanconsumer import (
    SpanConsumer,
    Incoming,
    Outgoing,
    ProcessorSettings,
    ProcessorError,
    SpanScribe,
    ConnectionSettings,
    QueueOptions,
    ProcessingOptions,
)


class TestConsumption:
    def test_basic_consume(self, test_consumer: SpanConsumer, input_queue_name):

        data = dict(key=None)

        @test_consumer.processor(in_key=input_queue_name)
        async def flip_value(incoming: Incoming):
            print("received value: ", incoming.media_loaded())
            data["key"] = incoming.media_loaded()

        assert data["key"] is None

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message={"ahhh": "AHHHH"})

        assert data["key"] == {"ahhh": "AHHHH"}

    def test_basic_consume_bytes(self, test_consumer: SpanConsumer, input_queue_name):

        data = dict(key=None)

        @test_consumer.processor(in_key=input_queue_name)
        async def flip_value(incoming: Incoming):
            print("received raw: ", incoming.message.body)
            print("received value: ", incoming.content)
            data["key"] = incoming.content

        assert data["key"] is None

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message=b"Hello")

        assert data["key"] == b"Hello"

    def test_basic_consume_model(self, test_consumer: SpanConsumer, input_queue_name):
        @dataclass
        class Name:
            first: str
            last: str

        @grahamcracker.schema_for(Name)
        class NameSchema(grahamcracker.DataSchema[Name]):
            pass

        data = dict(key=None)

        @test_consumer.processor(in_key=input_queue_name, in_schema=NameSchema())
        async def flip_value(incoming: Incoming):
            print("received value: ", incoming.media_loaded())
            data["key"] = incoming.media_loaded()

        assert data["key"] is None

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message=Name("Billy", "Peake"))

        assert data["key"] == Name("Billy", "Peake")

    def test_basic_pass_through(
        self, test_consumer: SpanConsumer, input_queue_name, output_queue_name
    ):
        @test_consumer.processor(in_key=input_queue_name, out_key=output_queue_name)
        async def flip_value(incoming: Incoming, outgoing: Outgoing):
            print("received value: ", incoming.media_loaded())
            outgoing.media = f"processed: {incoming.media_loaded()}"

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, "test value", mimetype=MimeType.TEXT)

            incoming = client.pull_message(output_queue_name)

            body = incoming.message.body.decode()
            assert body == f"processed: test value"
            assert incoming.media_loaded() == f"processed: test value"

    def test_return_message(
        self, test_consumer: SpanConsumer, input_queue_name: str, output_queue_name: str
    ):
        @test_consumer.processor(in_key=input_queue_name, out_key=output_queue_name)
        async def return_message(incoming: Incoming, outgoing: Outgoing):
            outgoing_message = aio_pika.Message(
                body=incoming.message.body,
                content_type=incoming.message.content_type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            outgoing.media = outgoing_message

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message="test", mimetype=MimeType.TEXT)
            result = client.pull_message(output_queue_name, max_empty_retries=1)
            assert result.message.body.decode() == "test"

    def test_consumption_concurrency(
        self, test_consumer: SpanConsumer, input_queue_name: str, output_queue_name: str
    ):
        test_consumer.prefetch_count = 100

        @test_consumer.processor(in_key=input_queue_name, out_key=output_queue_name)
        async def concurrent_sleep(incoming: Incoming, outgoing: Outgoing):
            index = incoming.media_loaded()
            await asyncio.sleep(0.1)
            test_consumer.logger.info(f"INDEX: {index}")
            outgoing.media = str(index)

        with test_consumer.test_client(delete_queues=True) as client:
            start = time.time()
            for i in range(100):
                client.put_message(routing_key=input_queue_name, message=str(i))

            responses = list()
            while True:
                try:
                    incoming = client.pull_message(
                        output_queue_name, max_empty_retries=4
                    )
                except aio_pika.exceptions.QueueEmpty:
                    break
                responses.append(incoming.message)

            elapsed = time.time() - start - 1
            print("ELAPSED: ", elapsed)
            response_indexes = [int(m.body.decode()) for m in responses]

            for i in range(100):
                assert i in response_indexes

            assert elapsed < 10

    def test_custom_mimetype(
        self, test_consumer: SpanConsumer, input_queue_name: str, output_queue_name: str
    ):
        def csv_encode(data: List[Dict[str, Any]]) -> bytes:
            encoded = io.StringIO()
            headers = list(data[0].keys())
            writer = csv.DictWriter(encoded, fieldnames=headers)
            writer.writeheader()
            writer.writerows(data)
            return encoded.getvalue().encode()

        def csv_decode(data: bytes) -> List[Dict[str, Any]]:
            csv_file = io.StringIO(data.decode())
            reader = csv.DictReader(csv_file)
            return [row for row in reader]

        test_consumer.register_mimetype(
            "text/csv", encoder=csv_encode, decoder=csv_decode
        )

        body_data = [
            {"first": "Harry", "last": "Potter"},
            {"first": "Hermione", "last": "Granger"},
        ]

        @test_consumer.processor(in_key=input_queue_name, out_key=output_queue_name)
        async def csv_handler(incoming: Incoming, outgoing: Outgoing):
            assert incoming.mimetype == "text/csv"
            media = incoming.media()
            assert media == body_data

            outgoing.mimetype = incoming.mimetype
            outgoing.media = media

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(
                routing_key=input_queue_name, message=body_data, mimetype="text/csv"
            )
            output = client.pull_message(routing_key=output_queue_name)
            assert output.mimetype == "text/csv"
            assert output.media() == body_data

    @pytest.mark.timeout(5)
    def test_requeue_once(self, test_consumer: SpanConsumer, input_queue_name: str):
        """Tests that a message can be redelivered on failure."""
        message_processed_counter = Counter()
        message_data = dict()

        @test_consumer.processor(
            in_key=input_queue_name,
            processing_options=ProcessingOptions(
                requeue=True, reject_on_redelivered=True
            ),
        )
        async def some_processor(incoming: Incoming):
            assert incoming.media_loaded() == "will error"
            message_processed_counter["result"] += 1
            message_data["redelivered"] = incoming.redelivered
            message_data["delivery_count"] = incoming.delivery_count
            raise ValueError

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, "will error")
            while message_processed_counter["result"] < 2:
                continue

        assert message_processed_counter["result"] == 2
        assert message_data["redelivered"] is True
        assert message_data["delivery_count"] == 1

    @pytest.mark.timeout(5)
    def test_requeue_multiple(self, test_consumer: SpanConsumer, input_queue_name: str):
        """Tests setting up a qurom queue with retry count set to 5"""
        message_processed_counter = Counter()
        message_data = dict()

        @test_consumer.processor(
            in_key=input_queue_name,
            queue_options=QueueOptions(arguments={"x-queue-type": "quorum"}),
            processing_options=ProcessingOptions(requeue=True, max_delivery_count=4),
        )
        async def some_processor(incoming: Incoming):
            message_processed_counter["result"] += 1
            assert incoming.media_loaded() == "will error"
            message_data["redelivered"] = incoming.redelivered
            message_data["delivery_count"] = incoming.delivery_count
            # Test that the value cache's correctly
            assert incoming.delivery_count == message_data["delivery_count"]
            raise ValueError

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, "will error")
            while message_processed_counter["result"] < 5:
                continue

        assert message_processed_counter["result"] == 5
        assert message_data["redelivered"] is True
        assert message_data["delivery_count"] == 4

    @pytest.mark.timeout(5)
    def test_reject_message(self, test_consumer: SpanConsumer, input_queue_name: str):
        """
        Tests that the user can reject messages if they would normally be re-queued.
        """
        message_processed_counter = Counter()
        message_data = dict()

        @test_consumer.processor(
            in_key=input_queue_name,
            queue_options=QueueOptions(arguments={"x-queue-type": "quorum"}),
            processing_options=ProcessingOptions(requeue=True, max_delivery_count=4),
        )
        async def some_processor(incoming: Incoming):
            message_processed_counter["result"] += 1
            message_data["redelivered"] = incoming.redelivered
            message_data["delivery_count"] = incoming.delivery_count
            incoming.reject = True
            raise ValueError

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, "will error")
            while message_processed_counter["result"] < 1:
                continue

        assert message_processed_counter["result"] == 1
        assert message_data["redelivered"] is False
        assert message_data["delivery_count"] == 0


class TestConsumptionErrors:
    def test_raises_handled(self, test_consumer: SpanConsumer, input_queue_name: str):
        value = dict(inc=0)
        value_lock = threading.Lock()

        @test_consumer.processor(in_key=input_queue_name)
        async def raise_value(incoming: Incoming):
            with value_lock:
                value["inc"] += 1
            raise ValueError("An error was raised!")

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message="test")
            client.put_message(input_queue_name, message="test")
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0.1), client.loop)

        assert value["inc"] == 2

    def test_raises_handled_custom(
        self, test_consumer: SpanConsumer, input_queue_name: str
    ):
        value = dict(inc=0)
        value_lock = threading.Lock()

        async def handle_route_error(error: BaseException, settings: ProcessorSettings):
            if isinstance(error, ProcessorError):
                with value_lock:
                    value["inc"] += int(str(error.error))

        test_consumer.add_error_handler(handle_route_error)

        @test_consumer.processor(in_key=input_queue_name)
        async def raise_value(incoming: Incoming):
            raise ValueError("2")

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message="test")
            client.put_message(input_queue_name, message="test")
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0.1), client.loop)

        assert value["inc"] == 4

    def test_raises_handled_custom_decorator(
        self, test_consumer: SpanConsumer, input_queue_name: str
    ):
        value = dict(inc=0)
        value_lock = threading.Lock()

        @test_consumer.on_error
        async def handle_route_error(error: BaseException, settings: ProcessorSettings):
            if isinstance(error, ProcessorError):
                with value_lock:
                    value["inc"] += int(str(error.error))

        @test_consumer.processor(in_key=input_queue_name)
        async def raise_value(incoming: Incoming):
            raise ValueError("2")

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message="test")
            client.put_message(input_queue_name, message="test")
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0.1), client.loop)

        assert value["inc"] == 4

    def test_raises_handled_custom_multiple(
        self, test_consumer: SpanConsumer, input_queue_name: str
    ):
        value = dict(inc=0)
        value_lock = threading.Lock()

        @test_consumer.on_error
        async def handle_route_error1(
            error: BaseException, settings: ProcessorSettings
        ):
            if isinstance(error, ProcessorError):
                with value_lock:
                    value["inc"] += float(str(error.error))

        @test_consumer.on_error
        async def handle_route_error2(
            error: BaseException, settings: ProcessorSettings
        ):
            if isinstance(error, ProcessorError):
                with value_lock:
                    value["inc"] += 0.1

        @test_consumer.processor(in_key=input_queue_name)
        async def raise_value(incoming: Incoming):
            raise ValueError("2.0")

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message="test")
            client.put_message(input_queue_name, message="test")
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0.1), client.loop)

        assert round(value["inc"], 1) == 4.2

    def test_raise_handler_fails(
        self, test_consumer: SpanConsumer, input_queue_name: str
    ):
        value = dict(inc=0)
        value_lock = threading.Lock()

        @test_consumer.on_error
        async def handle_route_error(error: BaseException, settings: ProcessorSettings):
            if isinstance(error, ProcessorError):
                with value_lock:
                    value["inc"] += int(str(error.error))
            raise ValueError("Well, fuck. Our last line of defence.")

        @test_consumer.processor(in_key=input_queue_name)
        async def raise_value(incoming: Incoming):
            raise ValueError("2")

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, message="test")
            client.put_message(input_queue_name, message="test")

        assert value["inc"] == 4


class TestTestClient:
    def test_delete_queues(
        self, test_consumer: SpanConsumer, input_queue_name: str, output_queue_name: str
    ):
        @test_consumer.processor(in_key=input_queue_name, out_key=output_queue_name)
        async def some_processor(incoming: Incoming, outgoing: Outgoing):
            print(incoming.media_loaded())
            outgoing.media = incoming.media_loaded()

        with test_consumer.test_client(delete_queues=True) as client:
            future = test_consumer.lifecycle.scribe.channel.declare_queue(
                output_queue_name, durable=True
            )

            declared: aio_pika.Queue = asyncio.run_coroutine_threadsafe(
                future, loop=client.loop
            ).result()

            assert declared.declaration_result.message_count == 0

            client.put_message(input_queue_name, message="test")
            client.put_message(input_queue_name, message="test")

            future = test_consumer.lifecycle.scribe.channel.declare_queue(
                output_queue_name, durable=True
            )
            declared: aio_pika.Queue = asyncio.run_coroutine_threadsafe(
                future, loop=client.loop
            ).result()

            assert declared.declaration_result.message_count != 0

        with test_consumer.test_client(delete_queues=True) as client:
            future = test_consumer.lifecycle.scribe.channel.declare_queue(
                output_queue_name, durable=True
            )
            declared: aio_pika.Queue = asyncio.run_coroutine_threadsafe(
                future, loop=client.loop
            ).result()

            assert declared.declaration_result.message_count == 0

    def test_pull_empty_queue(
        self, test_consumer: SpanConsumer, input_queue_name, output_queue_name
    ):
        @test_consumer.processor(in_key=input_queue_name, out_key=output_queue_name)
        async def some_processor(incoming: Incoming, outgoing: Outgoing):
            pass

        with test_consumer.test_client(delete_queues=True) as client:
            with pytest.raises(aio_pika.exceptions.QueueEmpty):
                client.pull_message(output_queue_name)

    def test_get_queue(
        self, test_consumer: SpanConsumer, input_queue_name: str, output_queue_name: str
    ):
        @test_consumer.processor(in_key=input_queue_name, out_key=output_queue_name)
        async def some_processor(incoming: Incoming, outgoing: Outgoing):
            outgoing.media = incoming.media_loaded()

        with test_consumer.test_client(delete_queues=True) as client:
            client.put_message(input_queue_name, "test")

            queue = client.get_queue(output_queue_name)
            future = asyncio.run_coroutine_threadsafe(queue.get(), client.loop)

            assert future.result().body.decode() == "test"


def raise_value_error(*args, **kwargs):
    raise ValueError


def publish_false(*args, **kwargs) -> bool:
    raise ValueError


class TestScribe:
    def test_runtime_error_no_connection(self):
        scribe = SpanScribe()
        loop = asyncio.get_event_loop()

        with pytest.raises(RuntimeError):
            loop.run_until_complete(scribe.get_channel())

        with pytest.raises(RuntimeError):
            loop.run_until_complete(scribe.get_queue("Test"))

    def test_get_queue_create_channel(self, connection_settings: ConnectionSettings):
        async def test():
            scribe = SpanScribe(settings=connection_settings)
            await scribe.connect_to_rabbit()
            assert scribe.channel is None

            queue = await scribe.get_queue("test_queue")
            assert isinstance(queue, aio_pika.Queue)
            assert isinstance(scribe.channel, aio_pika.Channel)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(test())

    def test_put_message_channel_create(self, connection_settings: ConnectionSettings):
        async def test():
            scribe = SpanScribe(settings=connection_settings)
            await scribe.connect_to_rabbit()
            assert scribe.channel is None

            await scribe.put_message("test_queue", "test")
            assert isinstance(scribe.channel, aio_pika.Channel)

            incoming = await scribe.pull_message("test_queue")
            assert incoming.media_loaded() == "test"

        loop = asyncio.get_event_loop()
        loop.run_until_complete(test())


class TestLifeCycle:
    def test_auto_name(self):
        service = SpanConsumer()

        assert isinstance(service.settings.name, str)
        uuid.UUID(service.settings.name)

    def test_double_consume_raises(self, test_consumer: SpanConsumer, input_queue_name):
        @test_consumer.processor(in_key=input_queue_name)
        async def flip_value(incoming: Incoming):
            pass

        with pytest.raises(ValueError):

            @test_consumer.processor(in_key=input_queue_name)
            async def flip_value_2(incoming: Incoming):
                pass

    def test_normal_startup(self, test_consumer: SpanConsumer):
        with pytest.raises(RuntimeError):
            _ = test_consumer.lifecycle

        with test_consumer.test_client(delete_queues=True) as client:
            assert client.consumer.lifecycle.event_startup.is_set()
            assert client.consumer.lifecycle.event_startup_complete.is_set()

            assert not test_consumer.lifecycle.event_shutdown.is_set()
            assert not test_consumer.lifecycle.event_shutdown_complete.is_set()

        assert test_consumer.lifecycle.scribe.connection is None
        assert test_consumer.lifecycle.event_shutdown.is_set()
        assert test_consumer.lifecycle.event_shutdown_complete.is_set()

    def test_startup_task(self, test_consumer: SpanConsumer, input_queue_name):
        value = dict(inc=0)

        async def task(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        test_consumer.add_startup_task(task)

        with test_consumer.test_client() as client:
            assert client.consumer.lifecycle.event_startup_complete.is_set()

        assert value["inc"] == 1

    def test_startup_task_decorator(self, test_consumer: SpanConsumer):
        value = dict(inc=0)

        @test_consumer.on_startup
        async def task(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        with test_consumer.test_client() as client:
            assert client.consumer.lifecycle.event_startup_complete.is_set()

        assert value["inc"] == 1

    def test_startup_task_multiple(self, test_consumer: SpanConsumer):
        value = dict(inc=0)

        @test_consumer.on_startup
        async def task1(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        @test_consumer.on_startup
        async def task2(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        with test_consumer.test_client(delete_queues=True) as client:
            assert client.consumer.lifecycle.event_startup_complete.is_set()

        assert value["inc"] == 2

    def test_startup_task_multiple_failure(self, test_consumer: SpanConsumer):
        """
        Tests that a failure on one startup task aborts the startup and signals a
        shutdown.
        """
        value = dict(inc=0)

        @test_consumer.on_startup
        async def task1(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1
            raise ValueError("Startup Task hit an error.")

        @test_consumer.on_startup
        async def task2(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        with test_consumer.test_client(delete_queues=True) as client:
            assert client.consumer.lifecycle.event_startup_complete.is_set()

        assert value["inc"] == 1
        assert test_consumer.lifecycle.event_startup_error.is_set()
        assert test_consumer.lifecycle.event_startup.is_set()
        assert test_consumer.lifecycle.event_startup_complete.is_set()
        assert test_consumer.lifecycle.event_shutdown_complete.is_set()

    def test_shutdown_task(self, test_consumer: SpanConsumer):
        """Tests that shutdown tasks are executed."""
        value = dict(inc=0)

        async def task(consumer: SpanConsumer):
            value["inc"] += 1

        test_consumer.add_shutdown_task(task)

        with test_consumer.test_client(delete_queues=True) as client:
            assert client.consumer.lifecycle.event_startup_complete.is_set()

        assert test_consumer.lifecycle.scribe.channel is None
        assert value["inc"] == 1

    def test_shutdown_task_decorator(self, test_consumer: SpanConsumer):
        value = dict(inc=0)

        @test_consumer.on_shutdown
        async def task(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        with test_consumer.test_client(delete_queues=True) as _:
            pass

        assert test_consumer.lifecycle.scribe.channel is None
        assert value["inc"] == 1

    def test_shutdown_task_double(self, test_consumer: SpanConsumer):
        value = dict(inc=0)

        @test_consumer.on_shutdown
        async def task1(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        @test_consumer.on_shutdown
        async def task2(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        with test_consumer.test_client(delete_queues=True) as _:
            pass

        assert test_consumer.lifecycle.scribe.channel is None
        assert value["inc"] == 2

    def test_shutdown_task_first_fails(self, test_consumer: SpanConsumer):
        value = dict(inc=0)

        @test_consumer.on_shutdown
        async def task1(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1
            raise ValueError()

        @test_consumer.on_shutdown
        async def task2(consumer: SpanConsumer):
            assert not consumer.lifecycle.scribe.connection.is_closed
            value["inc"] += 1

        with test_consumer.test_client(delete_queues=True) as _:
            pass

        assert test_consumer.lifecycle.scribe.channel is None
        assert value["inc"] == 2

    def test_double_startup(self, test_consumer: SpanConsumer):
        with pytest.raises(RuntimeError):
            _ = test_consumer.lifecycle

        with test_consumer.test_client(delete_queues=True) as _:
            assert test_consumer.lifecycle.scribe.channel is not None
            assert test_consumer.lifecycle.scribe.connection is not None
            assert test_consumer.lifecycle.event_startup.is_set()
            assert test_consumer.lifecycle.event_startup_complete.is_set()
            assert not test_consumer.lifecycle.event_shutdown.is_set()
            assert not test_consumer.lifecycle.event_shutdown_complete.is_set()

        assert test_consumer.lifecycle.scribe.channel is None
        assert test_consumer.lifecycle.event_startup.is_set()
        assert test_consumer.lifecycle.event_startup_complete.is_set()
        assert test_consumer.lifecycle.event_shutdown.is_set()
        assert test_consumer.lifecycle.event_shutdown_complete.is_set()

        with test_consumer.test_client(delete_queues=True) as _:
            assert test_consumer.lifecycle.scribe.channel is not None
            assert test_consumer.lifecycle.scribe.connection is not None
            assert test_consumer.lifecycle.event_startup.is_set()
            assert test_consumer.lifecycle.event_startup_complete.is_set()
            assert not test_consumer.lifecycle.event_shutdown.is_set()
            assert not test_consumer.lifecycle.event_shutdown_complete.is_set()

        assert test_consumer.lifecycle.scribe.channel is None
        assert test_consumer.lifecycle.event_startup.is_set()
        assert test_consumer.lifecycle.event_startup_complete.is_set()
        assert test_consumer.lifecycle.event_shutdown.is_set()
        assert test_consumer.lifecycle.event_shutdown_complete.is_set()

    def test_keyboard_interrupt(self, test_consumer: SpanConsumer, input_queue_name):
        async def raise_interrupt():
            raise KeyboardInterrupt

        @test_consumer.processor(in_key=input_queue_name)
        async def flip_value(incoming: Incoming,):
            pass

        with test_consumer.test_client(delete_queues=True) as client:
            asyncio.run_coroutine_threadsafe(raise_interrupt(), client.loop)
