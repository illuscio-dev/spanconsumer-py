.. automodule:: spanconsumer

Quick Start - Scribe
====================

The :class:`SpanScribe` class offers the core logic of sending and pulling messages.
This class offers the methods that :class:`SpanConsumer` is built off of, and is
designed to be a common interface shared between consumers and producers.

All Examples below show implementations involving producers, as it is recommended
consumer services be built around the :class:`SpanConsumer` framework rather than
invoking :class:`Spanscribe` itself.

RabbitMQ Connection Settings
----------------------------

If any of your connection settings are not the default, you will need to crete a
:class:`ConnectionSettings` object. All default values can remain unspecified.

.. code-block:: python

    from spanconsumer import ConnectionSettings

    rabbit_settings = ConnectionSettings(host="rabbit_service")
    print(rabbit_settings)

Output: ::

    ConnectionSettings(host='rabbit_service', port=5672, login='guest', password='guest')

Start Scribe Connection
-----------------------

Before the other methods will work, we need to connect to RabbitMQ via SpanScribe.
Remember we need to execute this inside an asyncio loop, since this is an asyncio
interface.

.. code-block:: python

    import asyncio
    import logging
    from spanconsumer import SpanScribe


    # This will be our main async function
    async def run_connection():
        scribe = SpanScribe(settings=rabbit_settings)
        await scribe.connect_to_rabbit()


    if __name__ == '__main__':

        # we can set the logger level to get more info about scribe's connections
        handler = logging.StreamHandler()
        handler.setLevel("INFO")

        logger = logging.root
        logger.addHandler(handler)
        logger.setLevel("INFO")

        # Starting the main asyncio loop
        loop = asyncio.get_event_loop()
        loop.run_until_complete(run_connection())

Output ::

    CONNECTION ESTABLISHED TO RABBITMQ

:class:`SpanScribe` will try forever to connect to RabbitMQ once a second until it
succeeds -- never throwing an error. It is designed to function in micro-service
environments where the messaging service availability may go out unexpectedly.


Put a Message
-------------

It's easy to send a message! No need to worry about the myriad of settings when all
you need to do is send message bodies to a queue! If queues are not explicitly,
declared they are generated in persistent mode on the default exchange. Most users will
not need to worry about the more intricate, advanced settings than this, so they are
handled for you.

.. code-block:: python

    async def run_connection():
        scribe = SpanScribe(settings=rabbit_settings)
        # Establish the connection
        await scribe.connect_to_rabbit()

        # scribe handles the broiler-plate of creating a channel and declaring a queue
        await scribe.put_message(routing_key="test_queue", message="OH HEY, MARK!")

:func:`SpanScribe.put_message` will automatically serialize the message if it is a
string, json-compatible dictionary, or bytes. All other types aren passed to ``str()``,
then encoded to bytes.

Output: ::

    CONNECTION ESTABLISHED TO RABBITMQ
    RABBITMQ CHANNEL ESTABLISHED
    RABBITMQ `test_queue` QUEUE DECLARED

Pull a Message
--------------

Let's fetch the message we just put on the queue:

.. code-block:: python

    async def run_connection():

        scribe = SpanScribe(settings=rabbit_settings)
        await scribe.connect_to_rabbit()

        # messages are returned in an Incoming object.
        incoming = await scribe.pull_message("test_queue")

        # We need to process this in a context block, or the message will not be removed
        # from the queue.
        with incoming.message.process():
            print(incoming.message)
            print("BODY:", incoming.body_data())

An :class:`Incoming` object is returned, which contains the message and a method for
automatically deserializing the message body.

Output: ::

    CONNECTION ESTABLISHED TO RABBITMQ
    ...
    IncomingMessage:{'app_id': None,
     'body_size': 13,
     'cluster_id': None,
     'consumer_tag': None,
     'content_encoding': '',
     'content_type': '',
     'correlation_id': None,
     'delivery_mode': 2,
     'delivery_tag': 1,
     'exchange': '',
     'expiration': None,
     'headers': {},
     'message_id': '37cebf918723bfbb0c7aad47d360d3a1',
     'priority': 0,
     'redelivered': False,
     'reply_to': None,
     'routing_key': 'test_queue',
     'timestamp': None,
     'type': 'None',
     'user_id': None}
    BODY: OH HEY, MARK!

**It is recommended that message pulling only be used in tests, and not to build
consumers on top of, as it is not an efficient implementation.**

Use a Marshmallow Schema
------------------------

Serializers and deserializers can also be marshmallow schemas.

.. code-block:: python

    class NameSchema(marshmallow.Schema):
        first = marshmallow.fields.Str(required=True)
        last = marshmallow.fields.Str(required=True)


    async def run_connection():
        scribe = SpanScribe(settings=rabbit_settings)
        await scribe.connect_to_rabbit()

        value_sent = {"first": "Harry", "last": "Potter"}
        await scribe.put_message("test_queue", message=value_sent, schema=NameSchema())

        incoming = await scribe.pull_message(
            "test_queue", schema=NameSchema()
        )

        with incoming.message.process():
            body = incoming.body_data()

            print("BODY:", body)
            print("BODY TYPE:", body.__class__)
            print("EQUALITY: ", body == value_sent)

Which means we get schema validation on deserialization! Remember that marshmallow does
not validate on ``Schema.dump(s)`` only ``Schema.load(s)``, so validation errors will
only be thrown when pulling information.

.. code-block:: python

    async def run_connection():
        scribe = SpanScribe(settings=rabbit_settings)
        await scribe.connect_to_rabbit()

        value_sent = {"first": "Harry"}
        await scribe.put_message(
            "test_queue", message=value_sent, schema=NameSchema()
        )

        incoming = await scribe.pull_message(
            "test_queue", schema=NameSchema()
        )

        with incoming.message.process():
            try:
                incoming.body_data()
            except marshmallow.ValidationError as error:
                print("ERROR:", error)

Output: ::

    CONNECTION ESTABLISHED TO RABBITMQ
    ...
    ERROR: {'last': ['Missing data for required field.']}
