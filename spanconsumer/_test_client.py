import threading
import asyncio
import aio_pika
import aiormq
from typing import Optional, Type, Any

# Need to import Literal from here for 3.7 compatibility.
from typing_extensions import Literal
from types import TracebackType

from spantools import MimeTypeTolerant, DataSchemaType

from ._incoming_outgoing import NOT_LOADED, Incoming
from ._scribe import SpanScribe


class TestClient:
    """
    Context manager for handling lifecycle of a consumer service in it's own thread when
    running tests.
    """

    def __init__(self, consumer: "SpanConsumer", delete_queues: bool):
        self.consumer: "SpanConsumer" = consumer
        """Consumer object the test client was spawned from."""
        self._worker_thread: threading.Thread
        """Thread that the consumer process is being run in."""
        self._flag_delete_queues: bool = delete_queues
        """Whether to delete the services queues on start."""

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """
        Asyncio loop shared with the consumer in another thread. This loop needs to
        be accessed in a thread safe way since the consumer is being run in another
        thread.
        """
        return self.consumer.loop

    @property
    def scribe(self) -> SpanScribe:
        return self.consumer.lifecycle.scribe

    @staticmethod
    def _run_consumer_in_thread(
        test_client: "TestClient", loop: asyncio.AbstractEventLoop
    ) -> None:
        """Function that the consumer thread runs."""
        asyncio.set_event_loop(loop)
        test_client.consumer.run()

    def get_queue(self, name: str) -> aio_pika.Queue:
        """
        Non-async way to get one of the consumer's queues for testing.

        :param name: Name of queue to fetch.
        :return: Requested queue.
        """
        coroutine = self.scribe.get_queue(routing_key=name)
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=5)

    def put_message(
        self,
        routing_key: str,
        message: Any = NOT_LOADED,
        mimetype: MimeTypeTolerant = None,
        **kwargs: Any,
    ) -> Optional[aiormq.types.ConfirmationFrameType]:
        """
        Non-async way to put a message to the consumer's queue.

        :param routing_key: key to put message to
        :param message: body of message to send or full message object. Bodies will be
            automatically serialized if possible.
        :param kwargs: if only the body is supplied, other keyword arguments can go
            here.
        :return: Confirmation from aio_pika.
        """
        processor_settings = self.consumer.settings.queue_processors[routing_key]

        coroutine = self.scribe.put_message(
            routing_key=routing_key,
            message=message,
            mimetype=mimetype,
            schema=processor_settings.in_schema,
            message_kwargs=kwargs,
        )

        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=5)

    def pull_message(
        self,
        routing_key: str,
        schema: Optional[DataSchemaType] = None,
        max_empty_retries: int = 0,
    ) -> Incoming:
        """
        Non-async way to pull message from the consumer's queue.

        :param routing_key: name of queue to pull from.
        :param schema: to deserialize message body.
        :param max_empty_retries: number of times to retry an empty queue. Retries are
            attempted once every 0.5 seconds.
        :return:
        """
        coroutine = self.scribe.pull_message(
            routing_key=routing_key, schema=schema, max_empty_retries=max_empty_retries
        )
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result()

    def __enter__(self) -> "TestClient":
        loop = asyncio.new_event_loop()

        if self._flag_delete_queues:
            loop.run_until_complete(self.delete_queues())

        self._worker_thread = threading.Thread(
            target=self._run_consumer_in_thread, args=(self, loop)
        )
        self._worker_thread.start()

        # We need to clear the last lifecycle so we don't his a race condition where
        # we are checking old events.
        try:
            self.consumer.reset()
        except RuntimeError:
            pass

        while True:
            try:
                self.consumer.lifecycle.event_startup_complete.wait()
            except RuntimeError:
                continue
            else:
                break

        return self

    async def delete_queues(self) -> None:
        """Deletes queues when exiting context block."""
        self.consumer.logger.info("TEST CLIENT DELETING RABBITMQ QUEUES")

        # We're going to boot up a temporary scribe so we don't need to start an entire
        # consumer to delete the queues.
        scribe = SpanScribe(settings=self.consumer.settings.connection)
        await scribe.connect_to_rabbit()
        await scribe.get_channel()

        # Try to delete the queues
        try:
            for setting in self.consumer.settings.queue_processors.values():
                await scribe.channel.queue_delete(setting.in_key)

                if setting.out_key is not None:
                    await scribe.channel.queue_delete(setting.out_key)
        finally:
            # Close the scribe on any errors
            await scribe.close_connection()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> Literal[False]:
        self.consumer.logger.info("SENDING SHUTDOWN SIGNAL")
        self.consumer.signal_shutdown()

        with self.consumer.lifecycle.worker_lock:
            pass

        self.consumer.logger.info("WORKERS DONE")
        self.consumer.lifecycle.event_shutdown_complete.wait()
        self.consumer.logger.info("SHUTDOWN COMPLETE")

        return False


typing_helper = False
if typing_helper:
    from ._consumer import SpanConsumer
