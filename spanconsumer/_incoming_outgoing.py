import aio_pika
from typing import Optional, Callable, Any, Union, TypeVar, Generic, cast, Dict
from dataclasses import dataclass, field

from spantools import (
    decode_content,
    encode_content,
    MimeType,
    MimeTypeTolerant,
    convert_params_headers,
    EncoderIndexType,
    DecoderIndexType,
    DataSchemaType,
)


MediaType = TypeVar("MediaType")
LoadedType = TypeVar("LoadedType")


class _NotLoadedFlag:
    pass


NOT_LOADED = _NotLoadedFlag()


@dataclass
class Incoming(Generic[MediaType, LoadedType]):
    message: aio_pika.IncomingMessage
    """Message from RabbitMQ"""
    schema: Optional[DataSchemaType]
    """Deserializer supplied to processor method."""
    _decoders: DecoderIndexType
    reject: bool = False
    """Set to true to reject message that would normally be requeued."""
    _media: Optional[Union[MediaType, _NotLoadedFlag]] = NOT_LOADED
    _media_loaded: Optional[Union[LoadedType, _NotLoadedFlag]] = NOT_LOADED
    _mimetype: MimeTypeTolerant = NOT_LOADED  # type: ignore
    _delivery_count: Optional[int] = None

    @property
    def mimetype(self) -> MimeTypeTolerant:
        if self._mimetype is NOT_LOADED:
            mimetype: MimeTypeTolerant = self.headers.get("Content-Type")
            if mimetype:
                try:
                    mimetype = MimeType.from_name(mimetype)
                except ValueError:
                    pass
            else:
                mimetype = None

            self._mimetype = mimetype

        return self._mimetype

    @property
    def content(self) -> Optional[bytes]:
        if not self.message.body:
            return None
        else:
            return self.message.body

    @property
    def headers(self) -> aio_pika.message.HeaderProxy:
        return self.message.headers

    def media(self) -> Optional[MediaType]:
        if self._media is NOT_LOADED:
            content: Optional[bytes] = self.content
            if content == b"":
                content = None

            if content is None:
                self._media = content
            else:
                self._media_loaded, self._media = decode_content(
                    content,
                    mimetype=self.mimetype,
                    allow_sniff=True,
                    data_schema=self.schema,
                    decoders=self._decoders,
                )

        self._media = cast(Optional[MediaType], self._media)
        return self._media

    @property
    def redelivered(self) -> bool:
        """Whether the message has been redelivered."""
        if self.message.redelivered:
            return True
        else:
            return False

    @property
    def delivery_count(self) -> int:
        """
        How many times the message has been re-delivered.

        NOTE: Only quorum queues fully track delivery count. For classic queues, the
        count will always be 1 if the message has been redelivered, regardless of the
        number of times it has been re-queued.
        """
        if self._delivery_count is not None:
            return self._delivery_count

        count: Optional[int] = self.message.headers.get("x-delivery-count")
        if count is None:
            if self.message.redelivered:
                count_result = 1
            else:
                count_result = 0
        else:
            count_result = count

        self._delivery_count = count_result
        return count_result

    def media_loaded(self) -> Optional[LoadedType]:
        """
        De-serializes message body
        """
        if self._media_loaded is NOT_LOADED:
            self.media()

        self._media_loaded = cast(Optional[LoadedType], self._media_loaded)
        return self._media_loaded


@dataclass
class Outgoing:
    _encoders: EncoderIndexType
    media: Any = NOT_LOADED
    """
    User-supplied body to serialize. Can also be pre-initialized ``aio_pika.Message``
    Defaults to "MISSING" object. No message will be sent if body is not set.
    """
    message_kwargs: Dict[str, Any] = field(default_factory=dict)
    """User-supplied keyword arguments to pass to ``aio_pika.Message``."""
    mimetype: MimeTypeTolerant = None
    """Mimetype to send body as."""
    headers: Dict[str, Any] = field(default_factory=dict)
    """Headers to add to message."""
    schema: Optional[Union[Callable, DataSchemaType]] = None
    """Serializer supplied to processor method."""

    def generate_message(self) -> aio_pika.Message:
        """
        Serializes message body and returns message. Default messages are persistent.

        :return:
        """
        if isinstance(self.media, aio_pika.Message):
            return self.media

        convert_params_headers(self.headers)

        body_bytes = encode_content(
            content=self.media,
            mimetype=self.mimetype,
            data_schema=self.schema,
            headers=self.headers,
            encoders=self._encoders,
        )

        message = aio_pika.Message(
            body=body_bytes,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type=self.headers.get("Content-Type"),  # type: ignore
            headers=self.headers,
            **self.message_kwargs,
        )

        return message
