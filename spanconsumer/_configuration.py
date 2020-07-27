from typing import Dict, Any, Optional
from dataclasses import dataclass, field, asdict
from spantools import DataSchemaType


from ._types import WrappedProcessorType, ProcessorType


@dataclass(frozen=True)
class ConnectionSettings:
    """
    Holds rabbitMQ connection settings for :class:`Scribe`. Default to default RabbitMQ
    settings.
    """

    host: str = "localhost"
    """Rabbit Server Host."""
    port: int = 5672
    """Rabbit Server Port."""
    login: str = "guest"
    """Rabbit Server Login."""
    password: str = "guest"
    """Rabbit Server Password."""


@dataclass
class QueueOptions:
    """Aio-Pika queue settings. Passed to ``aiopika.channel.declare_queue()`` as
    kwargs."""

    durable: bool = True
    """Whether the queue should store messages on disk."""
    passive: bool = False
    """Only check to see if queue exists."""
    auto_delete: bool = False
    """Delete queue when empty."""
    arguments: Optional[Dict[str, Any]] = None
    """Additional arguments to pass to the broker."""
    timeout: Optional[int] = None
    """Timout for queue declaration."""
    robust: bool = True
    """See aio-pika documentation."""


@dataclass
class ProcessingOptions:
    """
    Aio-Pika message handler settings. Passed to
    ``aiopika.IncomingMessage.__aenter__()`` context as kwargs.
    """

    requeue: bool = False
    """Requeue message on failure."""
    reject_on_redelivered: bool = False
    """Do no requeue if the message has already been requeued once."""
    ignore_processed: bool = False
    """Ignore message if it has been processed."""
    max_delivery_count: int = -1
    """
    ``x-delivery-count`` header value to stop re-queueing failed messages.

    ``-1`` is interpreted as no limit.

    NOTE: RabbitMQ Delivery counts start at ``0``. The second time the message is
    delivered it's delivery count is ``2``

    NOTE: This option has no effect on classic queues. Only quorum queues track delivery
    count.

    NOTE: This option has no effect if ``requeue`` is ``False``.

    NOTE: This option is not passed to aiopika, but handled directly by spanconsumer.
    """
    kwargs: Dict[str, Any] = field(init=False)
    """Kwargs used for aio pika message context. Not for setting by user."""

    def __post_init__(self) -> None:
        # Have to make a dummy value to use as-dict or the non-existent attr throws
        # an error
        self.kwargs = dict()
        self.kwargs = asdict(self)
        # max_delivery_count is not an aio-pika setting, but instead handled by the
        # consumer
        self.kwargs.pop("max_delivery_count")
        self.kwargs.pop("kwargs")


@dataclass(frozen=True)
class ProcessorSettings:
    """Settings for a decorated route processor function."""

    consumer: "SpanConsumer"
    """The parent consumer to run the processor."""
    worker: ProcessorType
    """The function passed into the decorator."""
    in_key: str
    """Rabbit routing key messages are pulled from."""
    out_key: Optional[str]
    """Rabbit routing key outgoing are sent to."""
    in_schema: Optional[DataSchemaType]
    """Function or schema to deserialize incoming message body."""
    out_schema: Optional[DataSchemaType]
    """Function or schema to deserialize outgoing message body."""
    processor: WrappedProcessorType = field(init=False)
    """Processor function to execute."""
    queue_options: Optional[QueueOptions] = None
    """Aio-Pika queue options."""
    processing_options: ProcessingOptions = field(default_factory=ProcessingOptions)
    """Aio-Pika message processing options."""


typing_help = False
if typing_help:
    from ._consumer import SpanConsumer
