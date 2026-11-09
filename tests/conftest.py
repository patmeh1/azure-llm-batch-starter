import pytest

from services.orchestrator.function_app import app


@pytest.fixture(scope="session")
def function_handlers():
    # Azure Functions indexing is stateful and must run only once per app instance.
    return {
        function.get_function_name(): function.get_user_function()
        for function in app.get_functions()
    }
