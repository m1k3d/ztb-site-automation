import sys
import json
from jinja2 import Environment, FileSystemLoader

# Usage: python3 render.py site_template.json.j2 vars-utrecht.json
if len(sys.argv) != 3:
    print("Usage: python3 render.py <template.j2> <vars.json>")
    sys.exit(1)

template_file = sys.argv[1]
vars_file = sys.argv[2]

# Load variables
with open(vars_file) as f:
    vars_data = json.load(f)

# Setup Jinja
env = Environment(loader=FileSystemLoader('.'))
env.filters['tojson'] = lambda v: json.dumps(v)

# Render template
template = env.get_template(template_file)
output = template.render(**vars_data)

# Print so it can be redirected to payload.json
print(output)