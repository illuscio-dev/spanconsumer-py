
.. automodule:: spanconsumer

SpanConsumer
============

Requirements
------------

- Python 3.7+

- SpanConsumer currently only supports `RabbitMQ`_ as its messaging backend, as it is
  built on top of `aio_pika`_

.. toctree::
    :maxdepth: 2
    :caption: Contents:

    ./quickstart_scribe.rst
    ./quickstart_consumer.rst
    ./api_doc.rst

``spanscribe`` is an async messaging and consumer library heavily inspired by the API
design of `responder`_ and `spanserver`_. It is intended to make designing consumer /
worker micro-services quick and easy.


.. _RabbitMQ: https://www.rabbitmq.com/
.. _aio_pika: https://aio-pika.readthedocs.io/en/latest/
.. _responder: https://python-responder.org/en/latest/
.. _spanserver: https://illuscio-dev-spanreed-py.readthedocs-hosted.com/en/latest/
