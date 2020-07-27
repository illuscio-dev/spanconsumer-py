import logging
import aio_pika
import aiormq
import asyncio
import copy
from dataclasses import asdict as dataclass_asdict
from typing import Optional, Any, Dict
from spantools import (
    MimeTypeTolerant,
    DecoderIndexType,
    EncoderIndexType,
    DEFAULT_DECODERS,
    DEFAULT_ENCODERS,
    EncoderType,
    DecoderType,
    MimeType,
    DataSchemaType,
)

from ._errors import ConfirmOutgoingError
from ._incoming_outgoing import Incoming, Outgoing
from ._configuration import QueueOptions, ConnectionSettings


class SpanScribe:
    """
    Core class for interacting with a aio_pika connection. This class is intended to
    be used by producers, and also supplies the underlying logic for
    :class:`SpanConsumer`. It allows both producers and consumers to share the same
    underlying logic.
    """

    def __init__(
        self,
        settings: Optional[ConnectionSettings] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        """
        :param settings: Settings class containing RabbitMQ connection info. If
            ``None``, default RabbitMQ settings are assumed.
        :param loop: Event loop to use in connection. If None, current event loop is
            used.
        """
        if settings is None:
            settings = ConnectionSettings()
        if loop is None:
            loop = asyncio.get_event_loop()

        self.settings: ConnectionSettings = settings
        """Settings class passed to init."""
        self.loop: asyncio.AbstractEventLoop = loop
        self.connection: aio_pika.Connection = None  # type: ignore
        self.channel: aio_pika.RobustChannel = None  # type: ignore
        """Async event loop."""
        self._queues: Dict[str, aio_pika.Queue] = dict()

        self._encoders: EncoderIndexType = copy.copy(DEFAULT_ENCODERS)
        self._decoders: DecoderIndexType = copy.copy(DEFAULT_DECODERS)

    async def connect_to_rabbit(self) -> aio_pika.Connection:
        """
        Try forever to connect to rabbit.
        """
        logger = logging.root

        while True:
            try:
                self.connection = await aio_pika.connect_robust(
                    host=self.settings.host,
                    port=self.settings.port,
                    login=self.settings.login,
                    password=self.settings.password,
                    loop=self.loop,
                )
            except (aiormq.exceptions.IncompatibleProtocolError, ConnectionError):
                logger.error("RABBITMQ CONNECTION FAILED. RETRYING IN 1S")
                await asyncio.sleep(1)
            else:
                logger.info("CONNECTION ESTABLISHED TO RABBITMQ")
                return self.connection

    async def close_connection(self) -> None:
        """Close RabbitMQ connection and clean up settings."""
        await self.connection.close()
        self.connection = None  # type: ignore
        self.channel = None  # type: ignore
        self._queues.clear()

    async def get_channel(self) -> aio_pika.RobustChannel:
        """Once Connected, get channel"""
        logger = logging.root

        try:
            self.channel = await self.connection.channel()
        except AttributeError:
            raise RuntimeError("Connection is not established")

        logger.info("RABBITMQ CHANNEL ESTABLISHED")
        return self.channel

    async def get_queue(
        self, routing_key: str, queue_options: Optional[QueueOptions] = None
    ) -> aio_pika.Queue:
        """
        Set up worker queue or get previously declared one using this connection.

        :param routing_key: of queue.
        :param queue_options: Options for pika queue creation.

        :return: Requested queue.
        """
        try:
            return self._queues[routing_key]
        except KeyError:
            pass

        if queue_options is None:
            queue_options = QueueOptions()

        queue_kwargs = dataclass_asdict(queue_options)

        logger = logging.root

        while True:
            try:
                queue = await self.channel.declare_queue(routing_key, **queue_kwargs)
            except AttributeError:
                if self.connection is None:
                    raise RuntimeError("Connection is not established")
                else:
                    await self.get_channel()
            else:
                logger.info(f"RABBITMQ `{routing_key}` QUEUE DECLARED")
                break

        self._queues[routing_key] = queue
        return queue

    async def pull_message(
        self,
        routing_key: str,
        schema: Optional[DataSchemaType] = None,
        max_empty_retries: int = 0,
        queue_options: Optional[QueueOptions] = None,
    ) -> Incoming:
        """
        Pull and deserialize message from RabbitMQ.

        :param routing_key: Queue to pull from.
        :param schema: Custom Schema or function to use when deserializing body.
        :param max_empty_retries: Maximum time to retry an empty queue. 0.5 seconds
            waited between each retry.
        :param queue_options: Options for pika queue creation.

        :return: Message, deserialized body.

        This method should NOT be used as a general way of fetching messages in
        production, but is included for testing purposes.
        """
        queue = await self.get_queue(routing_key, queue_options=queue_options)
        message = None
        tries = 0

        while message is None and tries <= max_empty_retries:
            message = await queue.get(fail=False)

            if message is None:
                await asyncio.sleep(0.5)

            tries += 1

        if message is None:
            raise aio_pika.exceptions.QueueEmpty

        incoming: Incoming = Incoming(
            message=message, schema=schema, _decoders=self._decoders
        )

        return incoming

    async def put_message(
        self,
        routing_key: str,
        message: Any,
        *,
        headers: Optional[Dict[str, Any]] = None,
        schema: Optional[DataSchemaType] = None,
        mimetype: MimeTypeTolerant = None,
        message_kwargs: Optional[Dict[str, Any]] = None,
        queue_options: Optional[QueueOptions] = None,
    ) -> Optional[aiormq.types.ConfirmationFrameType]:
        """
        Serialize and publish message to default exchange.

        :param routing_key: Queue to publish to.
        :param message: aio_pika.Message or body to be serialized into a message.
            Default messages are created in PERSISTENT delivery mode.
        :param headers: Headers to include in message.
        :param schema: Custom schema or function to use when serializing body.
        :param mimetype: Format to encode message.
        :param message_kwargs: Passed to ``aio_pika.Message`` when generating a message.
        :param queue_options: Options for pika queue creation.
        :return:
        """
        if not isinstance(message, Outgoing):
            if message_kwargs is None:
                message_kwargs = dict()
            if headers is None:
                headers = dict()

            message = Outgoing(
                _encoders=self._encoders,
                media=message,
                message_kwargs=message_kwargs,
                mimetype=mimetype,
                headers=headers,
                schema=schema,
            )

        output_message = message.generate_message()

        # Declare the queue if it doesnt exist. This will also create the channel if
        # needed.
        await self.get_queue(routing_key, queue_options=queue_options)

        assert self.channel.default_exchange is not None
        result = await self.channel.default_exchange.publish(
            message=output_message, routing_key=routing_key
        )

        if not result:
            raise ConfirmOutgoingError
        else:
            return result

    def register_mimetype(
        self, mimetype: MimeTypeTolerant, encoder: EncoderType, decoder: DecoderType
    ) -> None:
        """
        Registers encoder and decoder function for a given mimetype.

        :param mimetype: to register for ex: ``'text/csv'``.
        :param encoder: Encodes mimetype data to binary.
        :param decoder: Decodes mimetype data to binary.
        :return:
        """
        try:
            mimetype = MimeType.from_name(mimetype)
        except ValueError:
            pass

        self._encoders[mimetype] = encoder
        self._decoders[mimetype] = decoder
