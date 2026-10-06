"""List pagination for Chat Service.

Applied to every list endpoint from day one - see ADR-017. Adding pagination
after Step 262 freezes the v1 contract would be a BREAKING change, because the
response body changes from a JSON array to an object with a `results` key.
"""

from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
