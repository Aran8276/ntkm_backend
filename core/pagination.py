from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """``count``/``next``/``previous``/``results``; ``page_size`` is client-tunable (max 100)."""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminPagination(StandardPagination):
    page_size = 25
