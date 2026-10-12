"""Service discovery.

Chat REGISTERS itself here so that consumers (Cloud Gateway, Chat Messages
Service) can find it. It does NOT resolve its own dependencies through Eureka:
Auth and RabbitMQ are addressed by Docker service name (ADR-004, ADR-006), so
nothing on the request path depends on discovery being available. Step 260
revisits that for downstream addresses.
"""

