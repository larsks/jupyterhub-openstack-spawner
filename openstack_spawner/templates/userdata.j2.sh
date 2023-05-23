#!/bin/bash

cat > /etc/sysconfig/jupyterhub.env <<'EOF'
{% for varname, varval in env.items() -%}
{{varname}}={{varval}}
{% endfor -%}
EOF
