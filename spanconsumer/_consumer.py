import aio_pika
import asyncio
import threading
import logging
import sys
import uuid
import traceback
from spantools import EncoderType, DecoderType, MimeTypeTolerant, DataSchemaType
from typing import Dict, Callable, Optional, Union, List, Tuple, cast
from dataclasses import dataclass, field


from ._incoming_outgoing import Incoming, Outgoing, NOT_LOADED
from ._test_client import TestClient
from ._errors import ProcessorError, ConsumerStop
from ._scribe import SpanScribe, ConnectionSettings
from ._configuration import QueueOptions, ProcessingOptions, ProcessorSettings
from ._types import ProcessorType, WrappedProcessorType, TaskType, ErrorHandlerType


SHUTDOWN_ERRORS = (KeyboardInterrupt, ConsumerStop)


@dataclass
class Lifecycle:
    """Holds objects that only persist during a lifecycle."""

    scribe: SpanScribe
    """The scribe handling this incarnation of the service."""
    event_startup: threading.Event = field(default_factory=threading.Event)
    """Event set when the Consumer is started."""
    event_startup_complete: threading.Event = field(default_factory=threading.Event)
    """Event set when the Consumer startup completes successfully."""
    event_startup_error: threading.Event = field(default_factory=threading.Event)
    """Flag set to true if there is an error during startup."""
    event_shutdown: threading.Event = field(default_factory=threading.Event)
    """Event set when the Consumer shutdown starts."""
    event_shutdown_complete: threading.Event = field(default_factory=threading.Event)
    """Event set then the shutdown finished successfully."""
    event_signal_shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    """Use to signal that the service should be shut down."""
    event_loop_stopped: threading.Event = field(default_factory=threading.Event)
    """Used to signal that the loop has been stopped."""
    lock_shutdown: threading.Lock = field(default_factory=threading.Lock)
    """Threading lock held during a shutdown process."""
    worker_lock: threading.RLock = field(default_factory=threading.RLock)
    """The number of current workers handling requests."""
    task_shutdown: Optional[asyncio.Task] = None
    """Shut down asyncio task."""


# Because a new scribe is generated for each life-cycle of the app, we need to remember
# all the encoder to register upon each startup.
@dataclass
class MimeTypeRegistration:
    """Used to hold custom encoder / decoder information for the service."""

    mimetype: MimeTypeTolerant
    """Mimetype this encoder / decoder handles."""
    encoder: EncoderType
    """Encoder function."""
    decoder: DecoderType
    """Decoder function."""


@dataclass
class ConsumerSettings:
    name: str
    """Name of service passed to init. Filled with a random uuid if left blank."""
    connection: ConnectionSettings
    """Connection settings for service."""
    startup_tasks: List[TaskType]
    """"List of functions to run on startup"""
    shutdown_tasks: List[TaskType]
    """"List of functions to run on shutdown"""
    error_handlers: List[ErrorHandlerType]
    """Global error handler list"""
    prefetch_count: int
    """Number of messages the service will handle simultaneously."""
    queue_processors: Dict[str, ProcessorSettings]
    """Processor settings for each processor added to sthe service."""
    mimetype_handlers: List[MimeTypeRegistration]


class SpanConsumer:
    """Consumer service framework. Connects to rabbit and handles messages."""

    def __init__(
        self,
        name: Optional[str] = None,
        connection_settings: Optional[ConnectionSettings] = None,
        prefetch_count: int = 100,
    ) -> None:
        """
        :param name: for service.
        :param connection_settings: for rabbit connection. When None, default connection
            settings are used.
        :param prefetch_count: number of rabbit messages to process at once.
        """
        if name is None:
            name = str(uuid.uuid4())
        if connection_settings is None:
            connection_settings = ConnectionSettings()

        self.settings = ConsumerSettings(
            name=name,
            connection=connection_settings,
            startup_tasks=list(),
            shutdown_tasks=list(),
            error_handlers=list(),
            prefetch_count=prefetch_count,
            queue_processors=dict(),
            mimetype_handlers=list(),
        )
        """Consumer Service settings."""

        self._lifecycle: Optional[Lifecycle] = None
        """Holds resources for one run of the consumer."""

        self._logger: Optional[logging.Logger] = None
        """Holds logger for service."""

    @property
    def lifecycle(self) -> Lifecycle:
        """Returns the current or last used lifecycle."""
        if self._lifecycle is None:
            raise RuntimeError("Consumer has not been started.")
        return self._lifecycle

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """Event loop consumer is running on."""
        return self.lifecycle.scribe.loop

    @property
    def logger(self) -> logging.Logger:
        """Logger the service uses. Can be referenced by processors."""
        if self._logger is not None:
            return self._logger

        self._logger = logging.getLogger()
        if any(h.name == f"{self.settings.name}_INFO" for h in self._logger.handlers):
            return self._logger

        self._logger.setLevel(logging.INFO)

        formatter = logging.Formatter("INFO: %(message)s")

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        handler.name = f"{self.settings.name}_INFO"
        handler.setFormatter(formatter)
        self._logger.addHandler(handler)

        return self._logger

    def reset(self) -> None:
        """
        Reset service lifecycle after shutdown

        :raises RuntimeError: If service has not been shutdown.
        """
        if not self.lifecycle.event_shutdown_complete.is_set():
            raise RuntimeError("Cannot reset service that was not shutdown.")

        # Remove lifecycle.
        self._lifecycle = None

    async def _process_message(
        self, settings: ProcessorSettings, incoming: Incoming
    ) -> None:
        if settings.out_key:
            await settings.consumer.lifecycle.scribe.get_queue(settings.out_key)

        # set up args based on incoming / outgoing key declarations.
        if settings.out_key is not None:
            outgoing: Optional[Outgoing] = Outgoing(
                _encoders=self.lifecycle.scribe._encoders, schema=settings.out_schema
            )
            outgoing = cast(Outgoing, outgoing)
            args: Union[Tuple[Incoming], Tuple[Incoming, Outgoing]] = (
                incoming,
                outgoing,
            )
        else:
            outgoing = None
            args = (incoming,)

        try:
            await settings.worker(*args)  # type: ignore
        except BaseException as error:
            # Here we are going to raise an exception with the incoming and outgoing
            # objects so that the error handler has access to them.
            raise ProcessorError(error, incoming, outgoing)

        # Handle outgoing messages
        if outgoing is not None and outgoing.media is not NOT_LOADED:
            out_key = cast(str, settings.out_key)

            await settings.consumer.lifecycle.scribe.put_message(
                message=outgoing, routing_key=out_key
            )

    async def _process_message_context(
        self, settings: ProcessorSettings, message: aio_pika.IncomingMessage
    ) -> None:
        """
        Processes an incoming message, passing in incoming and outgoing objects, and
        handling any outgoing passing to the output message queue.
        """
        # Context block so the consumer can set ignore processed. NOTE: aio-pika has
        # a bas return type, so we need to manually set the more accurate type rather
        # than the generic 'Async Manager' it uses.
        message_context: aio_pika.message.ProcessContext = (
            message.process(**settings.processing_options.kwargs)  # type: ignore
        )

        async with message_context:
            reject = False

            incoming: Incoming = Incoming(
                message=message,
                schema=settings.in_schema,
                _decoders=self.lifecycle.scribe._decoders,
            )

            try:
                await self._process_message(settings, incoming)
            except BaseException as error:
                # Handle rejecting quorum queue messages that have hit the max error
                # count.
                max_count = settings.processing_options.max_delivery_count
                is_final_delivery = (
                    message.headers.get("x-delivery-count", 0)
                    >= settings.processing_options.max_delivery_count
                )

                # If we are using quorum queues hand have a max retry value, reject the
                # the message instead of re-queuing it.
                if is_final_delivery and max_count != -1:
                    reject = True

                raise error
            finally:
                if reject or incoming.reject:
                    # Tell the context the  message gas been processed so it doesn't
                    # throw an error.
                    message.reject()
                    message_context.ignore_processed = True

    async def _handle_processor_error(
        self, error: BaseException, processor_settings: ProcessorSettings
    ) -> None:
        """Handle error from a processor task."""
        if isinstance(error, ProcessorError):
            print_error = error.error
        else:
            print_error = error

        lines = traceback.format_exception(
            print_error.__class__, print_error, print_error.__traceback__
        )
        formatted_exc = "".join(lines)

        self.logger.error(
            f"ERROR AROUND '{processor_settings.worker.__name__}':\n\n"
            f"{formatted_exc}"
        )

        for handler in self.settings.error_handlers:
            try:
                await handler(error, processor_settings)
            except BaseException as new_error:
                lines = traceback.format_exception(
                    new_error.__class__, new_error, new_error.__traceback__
                )
                formatted_exc = "\n".join(lines)

                self.logger.error(
                    f"COULD NOT HANDLE '{error.__class__.__name__}' "
                    f"WITH '{handler.__name__}':\n\n"
                    f"{formatted_exc}"
                )

    def processor(
        self,
        *,
        in_key: str,
        out_key: Optional[str] = None,
        in_schema: Optional[DataSchemaType] = None,
        out_schema: Optional[DataSchemaType] = None,
        queue_options: Optional[QueueOptions] = None,
        processing_options: Optional[ProcessingOptions] = None,
    ) -> Callable:
        """
        Decorator for message processing function. Registers function with service.

        :param in_key: Input routing key / queue name to pull messages from.
        :param out_key: Output routing key / queue name to send messages to with
            outgoing object body as message body.
        :param in_schema: Schema or function to use when decoding incoming message body.
        :param out_schema: Schema or function to use when encoding outgoing message
            body.
        :param queue_options: Options for pika queue creation.
        :param processing_options: Options for pika message processing.

        Decorated functions must:

            - Return None.

            - Accept a :class:`Incoming` object as their first argument.

            - Accept a :class:`Outgoing` object as their second argument.

        No other arguments are passed to the function.
        """

        if processing_options is None:
            processing_options = ProcessingOptions()

        def decorator(worker: ProcessorType) -> WrappedProcessorType:
            """
            Decorator object. Wraps function.
            :param worker: Processor function.
            :return: Wrapped processor function.
            """
            if in_key in self.settings.queue_processors:
                raise ValueError(f"Processor for {in_key} exists")

            assert isinstance(processing_options, ProcessingOptions)

            settings = ProcessorSettings(
                consumer=self,
                worker=worker,
                in_key=in_key,
                out_key=out_key,
                in_schema=in_schema,
                out_schema=out_schema,
                queue_options=queue_options,
                processing_options=processing_options,
            )

            async def processor_wrapper(message: aio_pika.IncomingMessage) -> None:
                """Processes a rabbit message."""
                # Handle the message context so the decorated method doesn't have to
                # We are going to increment the worker lock on this thread. NOTE:
                # We will need to refactor this if multiprocess-based workers are
                # ever introduced.
                with self.lifecycle.worker_lock:
                    try:
                        await self._process_message_context(settings, message)
                    except BaseException as error:
                        await self._handle_processor_error(
                            error, processor_settings=settings
                        )

                    # Check to see if a stop order has been sent, if so we are going to
                    # exit this consumer.
                    if self.lifecycle.event_signal_shutdown.is_set():
                        self.logger.info("STOP REQUEST RECEIVED BY ASYNC WORKER.")
                        raise ConsumerStop("Consumer stop requested. Exiting worker.")

            object.__setattr__(settings, "processor", processor_wrapper)
            self.settings.queue_processors[in_key] = settings
            return processor_wrapper

        return decorator

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
        registration = MimeTypeRegistration(
            mimetype=mimetype, encoder=encoder, decoder=decoder
        )
        self.settings.mimetype_handlers.append(registration)

    def add_error_handler(self, handler: ErrorHandlerType) -> None:
        """
        Adds ``handler`` as error handler for errors that occur when processing a
        message.

        :param handler: Coroutine to add.
        """
        self.settings.error_handlers.append(handler)

    def on_error(self, handler: ErrorHandlerType) -> ErrorHandlerType:
        """
        Decorator that adds function as error handler.

        :param handler: Coroutine to add.
        :return:
        """
        self.add_error_handler(handler)
        return handler

    def add_startup_task(self, task: TaskType) -> None:
        """
        Adds ``task`` as function to be run during startup, after rabbit connection and
        queues are established.
        """
        self.settings.startup_tasks.append(task)

    def on_startup(self, task: TaskType) -> TaskType:
        """
        Decorator. Adds ``task`` as function to be run during startup, after rabbit
        connection and queues are established.
        """
        self.add_startup_task(task)
        return task

    def add_shutdown_task(self, task: TaskType) -> None:
        """
        Adds ``task`` as function to be run during shutdown, before rabbit connection
        is closed.
        """
        self.settings.shutdown_tasks.append(task)

    def on_shutdown(self, task: TaskType) -> TaskType:
        """
        Decorator. Adds ``task`` as function to be run during shutdown, before rabbit
        connection is closed.
        """
        self.add_shutdown_task(task)
        return task

    async def _run_startup_tasks(self) -> None:
        # We don't want to guard against errors here so we can abort startup if one of
        # the tasks runs into an error.
        for task in self.settings.startup_tasks:
            self.logger.info(f"PERFORMING TASK: {task.__name__}")
            await task(self)

    async def _run_shutdown_tasks(self) -> None:
        # Unlike startup tasks, we don't want to skip a shutdown task if a different
        # task runs into problems. We need to make sure that we can clean up as best we
        # can.
        for task in self.settings.shutdown_tasks:
            self.logger.info(f"PERFORMING TASK: {task.__name__}")
            try:
                await task(self)
            except BaseException as error:
                self.logger.error(
                    f"ERROR DURING SHUTDOWN TASK ({task.__name__}) "
                    f"{error.__class__.__name__}: {str(error)}\n"
                    f"STACK TRACE\n{traceback.format_exc()}"
                )

    async def _start_up(self) -> None:
        """Sets up service rabbit connection and queue consumers."""
        # Connect
        await self.lifecycle.scribe.connect_to_rabbit()

        # Creating channel
        await self.lifecycle.scribe.get_channel()

        # Maximum message count which will be
        # processing at the same time.
        await self.lifecycle.scribe.channel.set_qos(
            prefetch_count=self.settings.prefetch_count
        )

        # Declaring queues
        for name, settings in self.settings.queue_processors.items():
            queue = await self.lifecycle.scribe.get_queue(
                name, queue_options=settings.queue_options
            )
            await queue.consume(settings.processor)  # type: ignore

        await self._run_startup_tasks()
        self.lifecycle.event_startup_complete.set()

    async def _shutdown(self) -> None:
        """Handles shutdown tasks. Scheduled on startup and awaits shutdown signal."""
        # Wait for the shutdown signal to be sent
        await self.lifecycle.event_signal_shutdown.wait()
        self.lifecycle.event_shutdown.set()
        self.logger.info("SHUTDOWN SIGNAL RECEIVED")
        self.logger.info(f"SHUTTING DOWN '{self.settings.name}'")

        await self._run_shutdown_tasks()

        self.logger.info("CLOSING RABBITMQ CONNECTION")
        await self.lifecycle.scribe.close_connection()

        self.loop.stop()

    def signal_shutdown(self) -> None:
        """Signal the service to be shutdown."""
        logging.info("SIGNALING SHUTDOWN")
        self.loop.call_soon_threadsafe(self.lifecycle.event_signal_shutdown.set)

    def _clean_up_loop(self) -> None:
        """Cleans up event loop."""
        self.logger.info("CLEANING UP")

        tasks = asyncio.all_tasks(self.loop)
        for task in tasks:
            task.cancel()

        self.loop.close()

    def _main(self) -> None:
        self.logger.info("CONFIGURING LIFECYCLE")

        # Get the current event loop. NOTE: this loop will be closed upon service exit.
        loop = asyncio.get_event_loop()

        # Create a new lifecycle for this run. We create a lifecycle-per-run so that
        # lifecycle events are unique to each run and the entire event loop is cleaned
        # up and closed. This ensures that no dangling futures are executed on a restart
        # of the consumer.
        self._lifecycle = Lifecycle(scribe=SpanScribe(self.settings.connection, loop),)
        for handler in self.settings.mimetype_handlers:
            self._lifecycle.scribe.register_mimetype(
                mimetype=handler.mimetype,
                encoder=handler.encoder,
                decoder=handler.decoder,
            )

        # Signal that we are starting up.
        self.lifecycle.event_startup.set()

        # Run startup - makes a rabbitmq connection and runs startup tasks.
        try:
            self.logger.info(f"STARTING UP '{self.settings.name}'")
            self.loop.run_until_complete(self._start_up())
        except BaseException as error:
            self.logger.error("STARTUP FAILED", error)
            # Signal there was an error during startup.
            self.lifecycle.event_startup_error.set()
        finally:
            # Signal that startup has completed, regardless of whether there was a
            # startup error.
            self.lifecycle.event_startup_complete.set()

        # Schedule a task to wait on the shutdown signal.
        asyncio.ensure_future(self._shutdown(), loop=self.loop)

        # If there was an error during startup we need to abort and signal a shutdown.
        if self.lifecycle.event_startup_error.is_set():
            self.signal_shutdown()
        else:
            self.logger.info(f"RUNNING '{self.settings.name}'")

        # Run the loop. If we encountered an error during startup, this will trigger
        # an immediate shutdown.
        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            self.logger.info("KEYBOARD INTERRUPT ISSUED")
        finally:
            # At the end of the shutdown, we can clean up and close the loop.
            with self.lifecycle.lock_shutdown:
                self._clean_up_loop()
                self.logger.info("SHUT DOWN COMPLETE")

    def run(self) -> None:
        """Run the service."""
        # We need to handle potential errors here so we don't crash tests if something
        try:
            self._main()
        finally:
            # Signal that shutdown has fully completed.
            self.lifecycle.event_shutdown_complete.set()

    def test_client(self, delete_queues: bool = False) -> TestClient:
        """
        Returns test client for use in a context block for testing service.
        NOTE: The connection is not mocked. To make use of this client, a running rabbit
        connection must be reachable by the testing program.

        :param delete_queues: True: relevant queues are deleted before returning the
            test client so tests can be entered with a clean slate.

        :return:
        """
        return TestClient(self, delete_queues)
