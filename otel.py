from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter
)

# --------------------
# setup
# --------------------
resource = Resource(attributes={
    "service.name": "mini-agent"
})

provider = TracerProvider(resource=resource)

otlp_exporter = OTLPSpanExporter(
    endpoint="localhost:4317",
    insecure=True,   # 本地 Jaeger 不需要 TLS
)

processor = BatchSpanProcessor(otlp_exporter)

provider.add_span_processor(processor)

trace.set_tracer_provider(provider)

tracer = trace.get_tracer("mini-agent")