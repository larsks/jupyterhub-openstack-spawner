#!/bin/bash

useradd -m jones
cat > /etc/sudoers.d/jones <<EOF
jones ALL=(ALL) NOPASSWD: ALL
EOF
chmod 440 /etc/sudoers.d/jones

yum -y install git python3-pip nodejs-npm
pip3 install jupyterhub jupyterlab notebook
npm install -g configurable-http-proxy

cat > /etc/systemd/system/jupyterhub.service <<EOF
[Unit]
Description = JupyterHub single user
Wants = cloud-final.service
After = cloud-final.service

[Service]
Type = exec
User = jones
WorkingDirectory = /home/jones
EnvironmentFile = /etc/sysconfig/jupyterhub.env
ExecStart = /usr/local/bin/jupyterhub-singleuser --ip 0.0.0.0 --port 8000

[Install]
WantedBy = multi-user.target
EOF

systemctl enable jupyterhub.service
