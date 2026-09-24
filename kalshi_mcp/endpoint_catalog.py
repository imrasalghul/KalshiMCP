"""Structured endpoint metadata extracted from the supplied OpenAPI document."""
from pathlib import Path
import yaml

SPEC_PATH = Path(__file__).with_name('openapi.yaml')

def _load():
    with SPEC_PATH.open() as f:
        return yaml.safe_load(f)

def _catalog():
    spec = _load()
    result = {}
    for path, item in spec.get('paths', {}).items():
        for method, operation in item.items():
            if method.lower() not in {'get','post','put','patch','delete'}:
                continue
            op = operation or {}
            params = list(item.get('parameters', [])) + list(op.get('parameters', []))
            result[(method.upper(), path)] = {
                'operation_id': op.get('operationId'),
                'parameters': params,
                'request_body': op.get('requestBody'),
                'security': op.get('security', spec.get('security', [])),
                'responses': op.get('responses', {}),
            }
    return result

SPEC = _load()
ENDPOINT_METADATA = _catalog()
assert len(ENDPOINT_METADATA) == 116, len(ENDPOINT_METADATA)
ENDPOINTS = {key: value['operation_id'] for key, value in ENDPOINT_METADATA.items()}
