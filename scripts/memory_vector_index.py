"""Render an explicit embedding generation's Firebase index JSON; never deploy."""

import argparse
import json

from brainoid_memory.vector_store import EmbeddingConfig
from brainoid_persistence.vectors import index_spec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--dimensions", required=True, type=int)
    arguments = parser.parse_args()
    config = EmbeddingConfig(
        model=arguments.model, revision=arguments.revision, dimensions=arguments.dimensions
    )
    print(json.dumps(index_spec(config), indent=2))


if __name__ == "__main__":
    main()
