import asyncio
import time
import requests
import string
import random
import jinja2
from jupyterhub.spawner import Spawner
from traitlets import default, Unicode, List, Integer
from traitlets.config import Configurable
import openstack
import openstack.exceptions


class SpawnError(Exception):
    pass


class ServerCreationError(SpawnError):
    def __init__(self, msg, server):
        super().__init__(msg)
        self.server = server


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

    service_check_timeout = Integer(10, config=True)  # type: ignore

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.conn = openstack.connect(cloud=self.os_cloud_name)  # type: ignore
        self.userdata = UserdataGenerator(self, config=self.config)

        self.server_id = None
        self.server_name = self.make_server_name()

    def make_server_name(self):
        hash = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        return f"jhub-{self.user.name}-{hash}"

    async def create_server(self):
        loop = asyncio.get_running_loop()

        self.log.info(
            "creating server %s",
            self.server_name,
        )

        server = await loop.run_in_executor(
            None,
            lambda: self.conn.create_server(
                name=self.server_name,
                image=self.os_image_name,
                flavor=self.os_flavor_name,
                network=self.os_network_name,
                userdata=self.userdata.userdata,
                key_name=self.os_keypair_name,
                auto_ip=False,
                wait=False,
                tags=self.os_server_tags,
            ),
        )
        self.log.info("created server id %s", server.id)
        self.server_id = server.id

        # wait for server to become active
        self.log.info("wait for server to become active")
        while True:
            server = await self.get_server()
            if server is not None:
                if server.status == "ACTIVE":
                    break
                if server.status == "ERROR":
                    raise ServerCreationError("server entered ERROR state", server)

            await asyncio.sleep(1)

        self.log.info("server is active")
        return server

    async def assign_floating_ip(self, server):
        loop = asyncio.get_running_loop()
        floating_ip = await loop.run_in_executor(
            None, lambda: self.conn.available_floating_ip(self.os_floating_ip_network)
        )

        self.log.info("attaching floating ip %s", floating_ip.floating_ip_address)  # type: ignore
        await loop.run_in_executor(
            None,
            lambda: self.conn.add_ips_to_server(
                server, auto_ip=False, ips=[floating_ip.floating_ip_address]  # type: ignore
            ),
        )
        while True:
            server = await self.get_server()
            if server and server.public_v4:
                break
            await asyncio.sleep(1)

        self.log.info("floating ip %s is available", server.public_v4)
        return server

    async def start(self):
        try:
            server = await self.assign_floating_ip(await self.create_server())
            return f"http://{server.public_v4}:8000"
        except ServerCreationError as err:
            if "message" in err.server.get("fault", {}):
                msg = err.server.fault["message"]
            else:
                msg = str(err)
            self.log.error("failed to create server: %s", msg)
            await self.delete_server()
            raise

    async def get_server(self):
        if not self.server_id:
            return

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self.conn.get_server_by_id, self.server_id
        )

    async def server_active(self):
        server = await self.get_server()
        return server and server.status == "ACTIVE"

    async def service_is_available(self, server):
        loop = asyncio.get_running_loop()
        try:
            url = f"http://{server.public_v4}:8000{self.user.url}api"
            res = await loop.run_in_executor(
                None, lambda: requests.get(url, timeout=self.service_check_timeout)
            )
            if res.status_code == 200:
                self.log.info("poll: %s is available", url)
                return True
            self.log.info("poll: %s failed: %s", url, res.status_code)
        except Exception as err:
            self.log.info("poll: connection failed: %s", err)

        return False

    async def poll(self):
        server = await self.get_server()

        if server and server.status == "ACTIVE":
            self.log.info("poll: server is active")
            if server.public_v4:
                if await self.service_is_available(server):
                    return None
            else:
                self.log.info("poll: floating ip not yet available")
        else:
            self.log.info("poll: server not available or inactive")

        return 1

    async def delete_server(self):
        loop = asyncio.get_running_loop()
        if self.server_id:
            self.log.info("deleting server %s", self.server_id)
            if await loop.run_in_executor(
                None,
                lambda: self.conn.delete_server(
                    self.server_id,
                    delete_ips=True,
                    delete_ip_retry=5,
                    wait=True,
                ),
            ):
                while self.conn.get_server_by_id(self.server_id):
                    await asyncio.sleep(1)
            self.server_id = None

    async def stop(self):
        await self.delete_server()

    def get_state(self):
        state = super().get_state()

        if self.server_id:
            state["server_id"] = self.server_id
            state["server_name"] = self.server_name

        return state

    def load_state(self, state):
        super().load_state(state)
        if "server_id" in state:
            self.server_id = state["server_id"]
        if "server_name" in state:
            self.server_name = state["server_name"]

    def clear_state(self):
        super().clear_state()
        self.server_id = None
