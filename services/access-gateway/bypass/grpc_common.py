"""Shared limits for the Vinted gRPC transport."""

SERVICE_NAME = "vpars.access.v1.VintedAccess"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_TARGET = "127.0.0.1:50051"
