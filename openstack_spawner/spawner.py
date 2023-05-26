import asyncio
import time
import requests
import string
import random
import jinja2
from jupyterhub.spawner import Spawner
from traitlets import default, Unicode, List
from traitlets.config import Configurable
import openstack

# from openstack.compute.v2.server import Server as openstack_server


class UserdataGenerator(Configurable):
    userdata_template_module = Unicode(
        "openstack_spawner",  # type: ignore
        config=True,
    )

    userdata_template_name = Unicode(
        "userdata.j2.sh",  # type: ignore
        config=True,
    )

    def __init__(self, spawner, config=None):
        super().__init__(config=config)
        self.spawner = spawner
        self.templates = jinja2.Environment(
            loader=jinja2.PackageLoader(str(self.userdata_template_module))
        )

    @property
    def jupyter_env(self):
        """Return only those environment variables prefixed by `JUPYTERHUB_`"""

        env = {
            k: v
            for k, v in self.spawner.get_env().items()
            if k.startswith("JUPYTERHUB_")
        }

        env[
            "JUPYTERHUB_ACTIVITY_URL"
        ] = f"{env['JUPYTERHUB_API_URL']}/users/{self.spawner.user.name}/activity"

        return env

    @property
    def userdata(self):
        template = self.templates.get_template(str(self.userdata_template_name))
        return template.render(env=self.jupyter_env)


class OpenStackSpawner(Spawner):
    @default("ip")
    def _default_ip(self):
        return "0.0.0.0"

    @default("port")
    def _default_port(self):
        return 8000

    # Cloud name from clouds.yaml
    os_cloud_name = Unicode(config=True)

    # Ssh key to allow direct access to jupyterhub worker node
    os_keypair_name = Unicode(config=True)

    # Allocate float ips from this network
    os_floating_ip_network = Unicode(config=True)

    # Parameters for creating server
    os_flavor_name = Unicode(config=True)
    os_image_name = Unicode(config=True)
    os_network_name = Unicode(config=True)
    os_server_tags = List(config=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.conn = openstack.connect(cloud=self.os_cloud_name)  # type: ignore
        self.userdata = UserdataGenerator(self, config=self.config)
        self.server_id = None

    def create_server(self):
        hash = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        server_name = f"jhub-{self.user.name}-{hash}"
        self.log.info(
            "creating server %s",
            server_name,
        )

        # DO NOT set wait=True here because that will block the Jupyterhub
        # web ui until the server reaches ACTIVE state.
        server = self.conn.create_server(
            name=server_name,
            image=self.os_image_name,
            flavor=self.os_flavor_name,
            network=self.os_network_name,
            userdata=self.userdata.userdata,
            key_name=self.os_keypair_name,
            auto_ip=False,
            wait=True,
            tags=self.os_server_tags,
        )
        self.log.info("created server id %s", server.id)
        self.server_id = server.id

        # wait for server to become active
        self.log.info("waiting for server to become active")
        while not self.server_active():
            time.sleep(1)
        self.log.info("server is active")

        return server

    def assign_floating_ip(self, server):
        floating_ip = self.conn.available_floating_ip(self.os_floating_ip_network)

        self.log.info("attaching floating ip %s", floating_ip.floating_ip_address)  # type: ignore
        self.conn.add_ips_to_server(
            server, auto_ip=False, ips=[floating_ip.floating_ip_address]  # type: ignore
        )
        while True:
            server = self.get_server()
            if server and server.public_v4:
                break
            time.sleep(1)
        self.log.info("floating ip %s is available", server.public_v4)

        return server

    async def start(self):
        for attr in self.trait_names():
            if attr.startswith("os_"):
                self.log.info(f"{attr} = {getattr(self, attr)}")

        loop = asyncio.get_running_loop()
        server = await loop.run_in_executor(None, self.create_server)
        server = await loop.run_in_executor(None, self.assign_floating_ip, server)

        self.user.server.ip = server.public_v4  # type: ignore
        self.user.server.port = 8000  # type: ignore
        self.db.commit()

        return f"http://{server.public_v4}:8000"

    def get_server(self):
        if not self.server_id:
            return

        return self.conn.get_server_by_id(self.server_id)

    def server_active(self):
        server = self.get_server()
        return server and server.status == "ACTIVE"

    async def poll(self):
        server = self.get_server()

        if server and server.status == "ACTIVE":
            self.log.info("poll: server is active")
            if server.public_v4:
                try:
                    url = f"http://{server.public_v4}:8000{self.user.url}api"
                    res = requests.get(url)
                    if res.status_code == 200:
                        self.log.info("poll: %s is available", url)
                        return None
                    else:
                        self.log.info("poll: %s failed: %s", url, res.status_code)
                except Exception as err:
                    self.log.info("poll: connection failed: %s", err)
            else:
                self.log.info("poll: floating ip not yet available")
        else:
            self.log.info("poll: server not available or inactive")

        return 1

    async def stop(self):
        if self.server_id:
            self.log.info("deleting server %s", self.server_id)
            if self.conn.delete_server(
                self.server_id, delete_ips=True, delete_ip_retry=5
            ):
                while self.conn.get_server_by_id(self.server_id):
                    await asyncio.sleep(1)

    def get_state(self):
        state = super().get_state()

        if self.server_id:
            state["server_id"] = self.server_id

        self.log.info("save state: %s", state)
        return state

    def load_state(self, state):
        super().load_state(state)
        self.log.info("load state: %s", state)
        if "server_id" in state:
            self.server_id = state["server_id"]

    def clear_state(self):
        self.log.info("clear state")
        super().clear_state()
        self.server_id = None
