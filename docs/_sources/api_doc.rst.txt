.. automodule:: spanconsumer

API Documentation
=================

ConnectionSettings
-------------------
.. autoclass:: ConnectionSettings
    :members:

Scribe
------

.. autoclass:: SpanScribe
    :members:

SpanConsumer
------------

.. autoclass:: SpanConsumer
    :members:

TestClient
----------

.. autoclass:: TestClient
    :members:

Incoming / Outgoing
-------------------

.. autoclass:: Incoming
    :members:

.. autoclass:: Outgoing
    :members:


Queue Options
--------------

.. autoclass:: QueueOptions
    :members:


Processing Options
------------------

.. autoclass:: ProcessingOptions
    :members:


Processor Settings
------------------

.. autoclass:: ProcessorSettings
    :members:


MimeType
--------

.. autoclass:: MimeType
   :members:

   Enum class for the default supported Content-Types / Mimetypes for decoding and
   encoding.

   =========== ======================
   Enum Attr   Text Value
   =========== ======================
   JSON        application/json
   YAML        application/yaml
   BSON        application/bson
   TEXT        text/plain
   =========== ======================

   .. automethod:: is_mimetype

   .. automethod:: from_name

   .. automethod:: to_string

   .. automethod:: add_to_headers

   .. automethod:: from_headers

.. data:: MimeTypeTolerant

   Typing alias for ``Union[MimeType, str, None]``.

Exceptions
----------

.. autoexception:: ProcessorError
    :members:

.. autoexception:: ConfirmOutgoingError
    :members:

.. autoexception:: ConsumerStop
    :members:
