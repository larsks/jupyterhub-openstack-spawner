#!/bin/bash

cat > /etc/sysconfig/jupyterhub.env <<'EOF'
{% for varname, varval in env.items() -%}
{{varname}}={{varval}}
{% endfor -%}
EOF

. /etc/sysconfig/jupyterhub.env

user="${JUPYTERHUB_USER%@*}"

useradd -m $user
cat > /etc/sudoers.d/jupyterhub <<EOF
$user ALL=(ALL) NOPASSWD: ALL
EOF
chmod 440 /etc/sudoers.d/jupyterhub

cat > /etc/systemd/system/jupyterhub.service <<EOF
[Unit]
Description = JupyterHub single user for $user
Wants = cloud-final.service
After = cloud-final.service

[Service]
Type = exec
User = $user
WorkingDirectory = /home/$user
EnvironmentFile = /etc/sysconfig/jupyterhub.env
ExecStart = /usr/local/bin/jupyterhub-singleuser --ip 0.0.0.0 --port 8000

[Install]
WantedBy = multi-user.target
EOF

systemctl daemon-reload
systemctl enable jupyterhub.service
systemctl start --no-block jupyterhub.service
