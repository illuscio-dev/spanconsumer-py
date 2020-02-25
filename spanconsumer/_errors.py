from typing import Optional

from spantools import SpanError


class ProcessorError(SpanError):
    """Error occurred during supplied processor operation."""

    def __init__(
        self, error: BaseException, incoming: "Incoming", outgoing: Optional["Outgoing"]
    ) -> None:
        self.error: BaseException = error
        """Error raised in processor."""
        self.incoming: "Incoming" = incoming
        """Incoming object passed to processor."""
        self.outgoing: Optional["Outgoing"] = outgoing
        """Outgoing object passed to processor."""

    def __str__(self) -> str:
        return (
            f"Message Processor Error: "
            f"{self.error.__class__.__name__}: {str(self.error)}"
        )


class ConfirmOutgoingError(SpanError):
    """Broker did not confirm outgoing message"""


class ConsumerStop(SpanError):
    """Sent when exiting context block to halt running consumer."""


type_helper = False
if type_helper:
    from ._incoming_outgoing import Incoming, Outgoing  # noqa: F401
