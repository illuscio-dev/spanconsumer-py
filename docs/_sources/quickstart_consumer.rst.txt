.. automodule:: spanconsumer

Quick Start - Consumer Service
==============================

The examples below assume that you have rabbitmq running at it's default settings.

Set Up a Service
----------------

.. code-block:: python

    import asyncio
    from spanconsumer import SpanConsumer, Incoming, Outgoing

    service = SpanConsumer(name="Sleepy Greeter", prefetch_count=200)

    @service.processor(in_key="names")
    async def lazy_greet(incoming: Incoming):
       greeting = f"Hello, {incoming.message.body.decode()}"
       await asyncio.sleep(1)
       print(greeting)


The above service will wait for one second, then print a greeting based on messages it
receives from the routing key "names". We have told it to process 200 messages at a time
through the ``prefetch_count`` parameter.

The first parameter of the processor function is passed an :class`Incoming` object,
which contains an ``aio_pika.IncomingMessage`` on the ``message`` attribute.

Run the Service
---------------

Lets run our service:

.. code-block:: python

    service.run()

Output: ::

   INFO: STARTING UP 'Sleepy Greeter'
   INFO: CONNECTION ESTABLISHED TO RABBITMQ
   INFO: RUNNING 'Sleepy Greeter'

We can shut down the service with a keyboard interrupt and get the following output: ::

   INFO: KEYBOARD INTERRUPT ISSUED
   INFO: SHUTTING DOWN 'Sleepy Greeter'
   INFO: CLOSING RABBITMQ CONNECTION
   INFO: CLEANING UP
   INFO: SHUT DOWN COMPLETE

Test The Service
----------------

Now we need to test with some data. We could go through the manual overhead of invoking
pika, aio_pika, or some other library and set up a queue fo data, but that seems
tedious.

Spanscribe comes with an easy to use testing client that is accessed via
context-block:

.. code-block:: python

   with service.test_client(delete_queues=True) as client:
       client.put_message(routing_key="names", message="Dumbledore")
       client.put_message(routing_key="names", message="Severus Snape")

Which gives us the following output: ::

   INFO: STARTING UP 'Sleepy Greeter'
   INFO: CONNECTION ESTABLISHED TO RABBITMQ
   INFO: RUNNING 'Sleepy Greeter'
   Hello, Albus Dumbledore
   Hello, Severus Snape
   INFO: CONTEXT BLOCK EXITING
   INFO: STOP ORDER ISSUED
   INFO: SHUTTING DOWN 'Sleepy Greeter'
   INFO: CLOSING RABBITMQ CONNECTION
   INFO: CLEANING UP
   INFO: SHUT DOWN COMPLETE

The context block handles the spin-up, running, and shutdown of the service, as well as
gives us some methods to push data into the appropriate queues.

``delete_queues=True`` Tells the test client to delete all test queues before run, so
no dangling messages are left from previous tests.


Decode Message Bodies Automatically
-----------------------------------

The :class:`Incoming` object can automatically attempt to decode message bodies,
removing the need to write boiler-plate code.

.. code-block:: python

   @service.processor(in_key="names")
   async def lazy_greet(incoming: Incoming):
       greeting = f"Hello, {incoming.body_data()}"
       await asyncio.sleep(1)
       print(greeting)

Then add some test data while running it:

.. code-block:: python

   with service.test_client(delete_queues=True) as client:
       client.put_message(routing_key="names", message="Albus Dumbledore")
       client.put_message(routing_key="names", message="Severus Snape")

And get the same output: ::

    INFO: STARTING UP 'Sleepy Greeter'
    ...
    Hello, Dumbledor
    Hello, Severus Snape
    ...
    INFO: SHUT DOWN COMPLETE

Content-Type Encoding
---------------------

:class:`SpanConsumer` natively handles the following http body mimetypes:

    - JSON (application/json)
    - YAML (application/yaml)
    - BSON (application/bson)
    - TEXT (text/plain)

Each of the supported mimetypes is represented as a string Enum in :class:`MimeType`.

The mimetype of request body content is detailed on :func:`Request.mimetype`, which is
determined by the ``'Content-Type'`` header. If it is one of the content types detailed
above, a :class:`MimeType` enum value will be used, otherwise the raw text from the
header will be present.

.. code-block:: python

    import yaml
    from spanconsumer import MimeType,


    @service.processor(in_key="names")
    async def lazy_greet(incoming: Incoming):
        print("INCOMING MIMETYPE:", incoming.mimetype)
        media = incoming.media()

        greeting = f"Hello, {media['first']} {media['last']}"
        print(greeting)

    message_body = {"first": "Albus", "last": "Dumbledore"}

    with service.test_client(delete_queues=True) as client:
        client.put_message(
            routing_key="names", message=message_body, mimetype=MimeType.YAML
        )


Output: ::

    INFO: STARTING UP 'Sleepy Greeter'
    ...
    INCOMING MIMETYPE: MimeType.YAML
    Hello, Albus Dumbledore
    ...
    INFO: SHUT DOWN COMPLETE


.. note::

    When selecting a mimetype, spanconsumer is somewhat forgiving in how the mimetype
    is detailed. All of the following ``'Content-Type'`` values will be treated as
    ``MimeType.JSON``:

        - application/json
        - application/JSON
        - application/jSON
        - application/x-json
        - application/X-JSON
        - json
        - JSON

.. important::
    Non-native mimetypes like ``'text/csv'`` must be an exact string match.

Content-Type Sniffing
---------------------

When ``'Content-Type'`` is not specified in the request header, spanconsumer will
attempt to decode the content with every available decoder, until one does not throw
an error.

When registering custom encoders, make sure that they throw errors when they should.
For instance, json will decode raw strings to a str object, so the built-in json
decoder throws an error when the resulting object is not a dictionary or list.

Outgoing mimetype is resolved in the following order:

    1. Mimetype set to :func:`Outgoing.mimetype`
    2. JSON for ``dict`` / ``list`` media values
    3. TEXT for ``str`` media.
    4. No action for ``bytes`` media values.

Content-Type Handlers
---------------------

Custom encoders and decoders can be registered with the api, and can replace the default
ones. Let's register a couple to handle text/csv content.

An encoder must take in a data object and turn it into bytes:

.. code-block:: python

    import csv
    import io
    from typing import List, Dict, Any


    def csv_encode(data: List[Dict[str, Any]]) -> bytes:
        encoded = io.StringIO()
        headers = list(data[0].keys())
        writer = csv.DictWriter(encoded, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)
        return encoded.getvalue().encode()

Decoders must take in bytes and return python objects.

.. code-block:: python

    def csv_decode(data: bytes) -> List[Dict[str, Any]]:
        csv_file = io.StringIO(data.decode())
        reader = csv.DictReader(csv_file)
        return [row for row in reader]


Now we can register them with our api:

.. code-block:: python

    service.register_mimetype("text/csv", encoder=csv_encode, decoder=csv_decode)


Wala! Our consumer now understands how to encode and decode csv's

.. code-block:: python

    @service.processor(in_key="names")
    async def lazy_greet(incoming: Incoming):
        print("INCOMING MIMETYPE:", incoming.mimetype)
        print("MESSAGE RAW:", incoming.content)
        media = incoming.media()
        names = [f"{name['first']} {name['last']}" for name in media]
        greeting = f"Hello, {', '.join(names)}"
        print(greeting)

    body = [
        {"first": "Harry", "last": "Potter"}, {"first": "Hermione", "last": "Granger"}
    ]

    with service.test_client(delete_queues=True) as client:
        client.put_message(
            routing_key="names", message=body, mimetype="text/csv"
        )

Output: ::

    INFO: STARTING UP 'Sleepy Greeter'
    ...
    INCOMING MIMETYPE: text/csv
    MESSAGE RAW: b'first,last\r\nHarry,Potter\r\nHermione,Granger\r\n'
    Hello, Harry Potter, Hermione Granger
    ...
    INFO: SHUT DOWN COMPLETE

See Errors
----------

At a high level, errors are handled in the following way:

    - Errors that occur during startup cause the service to abort.

    - If a connection error occurs with RabbitMQ, the service will continue to attempt
      connecting forever until a connection can be re-established.

    - Errors that occur during the processing of a message are sent to the root logger
      and printed to stdout, but do not cause the service to fail.

.. code-block:: python

    service = SpanConsumer(name="Incrementally Interesting", prefetch_count=200)

    @service.processor(in_key="to_increment",)
    async def incrementer(incoming: Incoming):
        data = incoming.body_data()
        print("RESULT: ", int(data) + 1)

    with service.test_client(delete_queues=True) as client:
        client.put_message(routing_key="to_increment", message="one")
        client.put_message(routing_key="to_increment", message=2)

Output: ::

    INFO: STARTING UP 'Incrementally Interesting'
    ...
    INFO: ERROR AROUND 'incrementer':

    Traceback (most recent call last):
      File ".../spanscribe/_consumer.py", line 175, in _process_message
        await settings.worker(*args)  # type: ignore

      File "<input>", line 10, in incrementer

    ValueError: invalid literal for int() with base 10: 'one'

    RESULT:  3
    INFO: CONTEXT BLOCK EXITED
    ...
    INFO: SHUT DOWN COMPLETE

Handle Errors
-------------

You can add custom error handlers like so:

.. code-block:: python

    from spanscribe import ProcessorError, ProcessorSettings

    @service.on_error
    async def my_error_handler(error: BaseException, settings: ProcessorSettings):
        print("CUSTOM HANDLER")
        print(f"The Route was: {settings.in_key}")

        if isinstance(error, ProcessorError):
            print(f"The Message Body Was: {error.incoming.message.body.decode()}")
            print(f"The Error Was: {error.error}")

**Error handlers must be async coroutines**, and are passed two arguments:

    1. The ``BaseException`` object being raised.

    2. :class:`ProcessorSettings` for the processor coroutine the exception originated
       in.

Any errors that occur from within the processor itself will be wrapped in a
:class:`ProcessorError`, which will contain the :class:`Incoming` and :class:`Outgoing`
object, as well as the original error that was raised.

Output with out incrementer example above: ::

    INFO: STARTING UP 'Incrementally Interesting'
    ...
    INFO: ERROR AROUND 'incrementer':

    Traceback (most recent call last):

      File ".../spanscribe/_consumer.py", line 175, in _process_message
        await settings.worker(*args)  # type: ignore

      File "<input>", line 7, in incrementer

    ValueError: invalid literal for int() with base 10: 'one'

    CUSTOM HANDLER
    The Route was: to_increment
    The Message Body Was: one
    The Error Was: invalid literal for int() with base 10: 'one'
    RESULT:  3
    INFO: CONTEXT BLOCK EXITED
    ...
    INFO: SHUT DOWN COMPLETE

Multiple error handlers can be added, **but error handler execution order is not
guaranteed.**

Error handlers can also be added without the decorator:

.. code-block:: python

    async def my_error_handler(error: BaseException, settings: ProcessorSettings):
        # some logic
        ...

    service.add_error_handler(my_error_handler)


Send Results to an Output Queue
-------------------------------

We can add an output queue to our greeting processor, then pass results to an
:class:`Outgoing` object:

.. code-block:: python

   @service.processor(in_key="names", out_key="greetings")
   async def lazy_greet(incoming: Incoming, outgoing: Outgoing):
       greeting = f"Hello, {incoming.body_data()}"
       await asyncio.sleep(1)
       outgoing.body = greeting

And then test it:

.. code-block:: python

   with service.test_client(delete_queues=True) as client:
      # Put our test data.
      client.put_message(routing_key="names", message="Albus Dumbledore")
      client.put_message(routing_key="names", message="Severus Snape")

      # Fetch our results. This method returns a Message, deserialized body tuple.
      message1, greeting1 = client.pull_message("greetings", max_empty_retries=10)
      message2, greeting2 = client.pull_message("greetings", max_empty_retries=10)

   print(message1.body.decode())
   print(greeting1)

   print(message2.body.decode())
   print(greeting2)

Output: ::

    INFO: STARTING UP 'Sleepy Greeter'
    ...
    INFO: SHUT DOWN COMPLETE
    Hello, Albus Dumbledore
    Hello, Albus Dumbledore
    Hello, Severus Snape
    Hello, Severus Snape

Use Marshmallow Schemas
-----------------------

`Marshmallow`_ Schemas can be used as both encoders and decoders. Lets alter our lazy
greeter again.

.. code-block:: python

    import marshmallow
    from typing import Dict


    class WizardSchema(marshmallow.Schema):
        first = marshmallow.fields.Str(required=True)
        last = marshmallow.fields.Str(required=True)
        house = marshmallow.fields.Str(required=True)

        @marshmallow.validates("house")
        def house_exists(self, value: str):
            if value not in ["Gryffindor", "Slytherin", "Hufflepuff", "Ravenclaw"]:
                raise marshmallow.ValidationError(
                    f"{value} is not a valid Hogwarts House"
                )


    @service.processor(in_key="wizards", in_schema=WizardSchema(), out_key="greetings")
    async def lazy_greet(incoming: Incoming, outgoing: Outgoing):
        wizard: Dict[str, str] = incoming.body_data()
        greeting = (
            f"Hello, {wizard['first']} {wizard['last']} of House {wizard['house']}"
        )

        await asyncio.sleep(1)
        outgoing.body = greeting

Now our service can handle schema validation of message bodies!

.. code-block:: python

    neville = dict(first="Neville", last="Longbottom", house="Hufflepuff")
    malfoy = dict(first="Draco", house="Slytherin")
    billy = dict(first="Billy", last="Peake", house="Unicorn")

    with service.test_client(delete_queues=True) as client:
        # Put our test data.
        client.put_message(routing_key="wizards", message=malfoy)
        client.put_message(routing_key="wizards", message=billy)
        client.put_message(routing_key="wizards", message=neville)

        # Fetch our results (Two of the above will throw errors).
        _, greeting1 = client.pull_message("greetings", max_empty_retries=10)

    print("FETCHED: ", greeting1)

Output: ::

    INFO: STARTING UP 'Sleepy Greeter'
    ...
    INFO: ERROR AROUND 'lazy_greet':

    Traceback (most recent call last):
        ...
    marshmallow.exceptions.ValidationError: {'last': ['Missing data for required field.']}

    INFO: ERROR AROUND 'lazy_greet':

    Traceback (most recent call last):
        ...
    marshmallow.exceptions.ValidationError: {'house': ['Unicorn is not a valid Hogwarts House']}

    INFO: CONTEXT BLOCK EXITED
    ...
    INFO: SHUT DOWN COMPLETE
    FETCHED:  Hello, Neville Longbottom of House Hufflepuff

Schemas can also be used as an output. Output schemas invoke the
`marshmallow.Schema.dump`_ method before serialization.

Queue Options
-------------

You can pass queue declaration settings through the :class:`QueueOptions` class. These
options are passed to ``aio_pika.RobustChannel.declare_queue()`` as kwargs.

.. code-block:: python

    import asyncio
    from spanconsumer import SpanConsumer, Incoming, Outgoing

    service = SpanConsumer(name="Sleepy Greeter", prefetch_count=200)

    @service.processor(
        in_key="names",
        queue_options=QueueOptions(arguments={"x-queue-type": "quorum"}),
    )
    async def lazy_greet(incoming: Incoming):
        ...


We've declared a quorum queue using queue arguments!


Processing Options
------------------

You can pass message handling settings to :class:`ProcessingOptions`. These options
are passed to ``aiopika.IncomingMessage.__aenter()`` as kwargs in a context block.

.. code-block:: python

    @service.processor(
        in_key="names",
        processing_options=ProcessingOptions(requeue=True, reject_on_redelivered=True),
    )
    async def lazy_greet(incoming: Incoming):
        ...


Our consumer will now requeue failed messages when an error occurs and reject previously
failed messages on an error.

(This combination can be used to retry failed messages once in classic queue.)


Run Startup / Shutdown Tasks
------------------------------

Startup and shutdown tasks can be added like error handlers:

.. code-block:: python

    @service.on_startup
    async def startup_task(consumer: SpanConsumer):
        consumer.logger.info("OH BOY! HERE I GO GREETING AGAIN!")


    @service.on_shutdown
    async def shutdown_task(consumer: SpanConsumer):
        consumer.logger.info("GOODNIGHT!")


**Startup tasks**:

    - Must accept one argument: the consumer running them.

    - Must be asyncio coroutines.

    - Run after the RabbitMQ connection has been established.

    - Run after the consumer's logger has been primed.

    - Are not guaranteed to run in the order they were added.

    - Will cause the consumer to abort startup if they throw an exception

**Shutdown tasks**:

    - Must accept one argument: the consumer running them.

    - Must be asyncio coroutines.

    - Run before the RabbitMQ connection has been closed.

    - Are not guaranteed to run in the order they were added.

    - Are ignored when an exception is thrown. The exception is logged and shutdown
      continues as normal.

Now when we run the service:

.. code-block:: python

    with service.test_client(delete_queues=True) as _:
        pass

Output: ::

    INFO: STARTING UP 'Sleepy Greeter'
    INFO: CONNECTION ESTABLISHED TO RABBITMQ
    INFO: PERFORMING TASK: startup_task
    INFO: OH BOY! HERE I GO GREETING AGAIN!
    INFO: RUNNING 'Sleepy Greeter'
    INFO: CONTEXT BLOCK EXITED
    INFO: SHUTTING DOWN 'Sleepy Greeter'
    INFO: PERFORMING TASK: shutdown_task
    INFO: GOODNIGHT!
    INFO: CLOSING RABBITMQ CONNECTION
    INFO: CLEANING UP
    INFO: SHUT DOWN COMPLETE

Like error handlers, tasks can be added without the decorator:

.. code-block:: python

    async def startup_task(consumer: SpanConsumer):
        consumer.logger.info("OH BOY! HERE I GO GREETING AGAIN!")


    async def shutdown_task(consumer: SpanConsumer):
        consumer.logger.info("GOODNIGHT!")


    service.add_startup_task(startup_task)
    service.add_shutdown_task(shutdown_task)

.. _RabbitMQ: https://www.rabbitmq.com/
.. _aio_pika: https://aio-pika.readthedocs.io/en/latest/
.. _responder: https://python-responder.org/en/latest/
.. _Marshmallow: https://marshmallow.readthedocs.io/en/latest/
.. _marshmallow.Schema.dump: https://marshmallow.readthedocs.io/en/latest/api_reference.html#schema
