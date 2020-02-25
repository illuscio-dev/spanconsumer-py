import aio_pika
from typing import Callable, Any, Optional, Union, Coroutine

from ._incoming_outgoing import Incoming, Outgoing


ProcessorType = Union[
    Callable[[Incoming], Coroutine[Any, Any, None]],
    Callable[[Incoming, Optional[Outgoing]], Coroutine[Any, Any, None]],
]
WrappedProcessorType = Callable[[aio_pika.IncomingMessage], Coroutine[Any, Any, None]]
ErrorHandlerType = Callable[
    [BaseException, "ProcessorSettings"], Coroutine[Any, Any, None]
]
TaskType = Callable[["SpanConsumer"], Coroutine[Any, Any, None]]


typing_help = False
if typing_help:
    from ._configuration import ProcessorSettings  # noqa: F401
    from ._consumer import SpanConsumer  # noqa: F401
