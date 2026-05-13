from opentelemetry import trace

from opentelemetry.sdk.trace import TracerProvider

from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

# Create provider
provider = TracerProvider()

# Export spans to stdout
processor = BatchSpanProcessor(
    ConsoleSpanExporter()
)

provider.add_span_processor(processor)

trace.set_tracer_provider(provider)

# Global tracer
tracer = trace.get_tracer("mini-agent")