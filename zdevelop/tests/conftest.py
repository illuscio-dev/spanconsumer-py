import pytest
import asyncio

from spanconsumer import SpanConsumer, ConnectionSettings


@pytest.fixture
def input_queue_name() -> str:
    return "test_tasks"


@pytest.fixture
def output_queue_name() -> str:
    return "test_results"


@pytest.fixture()
def connection_settings() -> ConnectionSettings:
    settings = ConnectionSettings(port=57018)
    return settings


@pytest.fixture
def test_consumer(
    input_queue_name, output_queue_name, connection_settings
) -> SpanConsumer:

    consumer = SpanConsumer(
        name="Test Service", connection_settings=connection_settings, prefetch_count=10
    )

    return consumer
