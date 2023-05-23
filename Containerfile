FROM docker.io/jupyterhub/jupyterhub:4 AS build

RUN apt-get update
RUN apt-get -y install \
	git \
	python3-dev \
	python3-venv \
	build-essential

WORKDIR /app
RUN python3 -mvenv .venv
RUN . .venv/bin/activate && pip3 install openstacksdk
COPY requirements.txt ./
RUN . .venv/bin/activate && pip3 install -r requirements.txt

FROM docker.io/jupyterhub/jupyterhub:4

COPY --from=build /app /app

ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin

WORKDIR /app
COPY . ./
RUN . .venv/bin/activate && pip3 install -e .

WORKDIR /srv/jupyterhub
ENTRYPOINT ["jupyterhub"]
