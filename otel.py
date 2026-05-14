from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

import json
import os

OTEL_FILE = "otel_trace.jsonl"


class OverwriteFileSpanExporter(SpanExporter):
    def __init__(self, filename):
        self.filename = filename
        # 👉 启动时清空文件（关键）
        open(self.filename, "w").close()

    def export(self, spans):
        with open(self.filename, "a") as f:
            for span in spans:
                f.write(json.dumps({
                    "name": span.name,
                    "context": str(span.context),
                    "attributes": dict(span.attributes or {}),
                    "status": str(span.status),
                }, default=str) + "\n")

        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


# --------------------
# setup
# --------------------
provider = TracerProvider()

processor = BatchSpanProcessor(
    OverwriteFileSpanExporter(OTEL_FILE)
    # ConsoleSpanExporter()
    
)

provider.add_span_processor(processor)

trace.set_tracer_provider(provider)

tracer = trace.get_tracer("mini-agent")