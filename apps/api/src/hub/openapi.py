"""Print the API's OpenAPI document (`make web-types`; the web app generates its types from it).

Builds the app with an empty container: no database, no model, no `.env`, so it runs anywhere
and the document does not depend on local settings.
"""

import json
import sys

from hub.config import Settings
from hub.deps import Container
from hub.main import create_app


def document() -> dict[str, object]:
    app = create_app(Container(settings=Settings(_env_file=None, app_env="test")))
    return app.openapi()


def main() -> None:
    json.dump(document(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
