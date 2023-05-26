#!/bin/bash

yum -y install git python3-pip nodejs-npm
pip3 install jupyterhub jupyterlab notebook
npm install -g configurable-http-proxy
