#!/usr/bin/env python
# -*- coding: utf-8 -*-

from fabric import Connection
from os import listdir, makedirs
from os.path import isfile, isdir
import yaml
from jinja2 import Environment, FileSystemLoader
from threading import Thread
import shutil
import sys


class ClusterManager:
    def __init__(self, service_name, node_manager_class, nodes_file='nodes.yaml'):
        with open(nodes_file, 'r') as input_file:
            props = yaml.safe_load(input_file)
        props['service_name'] = service_name

        # Update global props with service props for direct access
        try:
            props.update(props['services'][service_name]['props'])
        except KeyError:
            pass

        conn_props = {
            'port': 22,
            'user': props['user'],
            'connect_kwargs': {
                "key_filename": props['keyfile']
            }
        }

        self.node_managers = []
        for index, node in enumerate(props['services'][service_name]['nodes']):
            node_conn_props = dict(conn_props)
            node_conn_props['host'] = node['public_address']

            node_props = dict(props)
            node_props['index'] = index
            node_props['id'] = index + 1

            node_manager = node_manager_class(node_props, node_conn_props)
            self.node_managers.append(node_manager)

    def deploy(self):
        self._run_parallel('deploy')

    def start(self):
        self._run_parallel('start')

    def destroy(self):
        self._run_parallel('destroy')

    def clean_data(self):
        self._run_parallel('clean_data')

    def _run_parallel(self, target):
        threads = []
        for node_manager in self.node_managers:
            threads.append(Thread(target=getattr(node_manager, target)))
            threads[-1].start()
        for thread in threads:
            thread.join()


class NodeManager:
    def __init__(self, props, conn_props):
        props['node'] = props['services'][props['service_name']]['nodes'][props['index']]
        self.props = props
        self.conn = Connection(**conn_props)

    def deploy(self):
        self.pull()
        self.run()
        self.configure()

    def pull(self):
        self.conn.run(f"docker pull {self.props['container_image']}")

    def run(self):
        raise NotImplementedError

    def stop(self):
        self.conn.run(f"docker stop {self.props['container_name']}")

    def destroy(self):
        try:
            self.stop()
            try:
                self.conn.run(f"docker rm -f {self.props['container_name']}")
            except Exception:
                return
        except Exception:
            return

    def configure(self):
        self._prepare_local_env()
        self._rendered_templates()
        self._remove_host_tmp_path()
        self._make_host_tmp_path()
        self._copy_files_to_host()
        self._copy_files_to_container()

    def clean_data(self):
        if 'host_data_path' in self.props:
            self.conn.run(f"sudo rm -rf {self.props['host_data_path']}")
        if 'host_logs_paths' in self.props:
            self.conn.run(f"sudo rm -rf {self.props['host_logs_paths']}")

    def start(self):
        raise NotImplementedError

    def _get_rendered_service_dir_path(self):
        return f"{self._get_rendered_dir_path()}{self.props['service_name']}/"

    def _get_rendered_dir_path(self):
        return f"{self.props['local_conf_path']}rendered/node{self.props['id']}/"

    def _prepare_local_env(self):
        conf_dir_path = f"{self.props['local_conf_path']}{self.props['service_name']}/"
        rendered_service_dir_path = self._get_rendered_service_dir_path()

        try:
            makedirs(rendered_service_dir_path)
        except FileExistsError:
            pass

        try:
            shutil.rmtree(rendered_service_dir_path)
        except FileNotFoundError:
            pass

        shutil.copytree(conf_dir_path, rendered_service_dir_path)

    def _rendered_templates(self):
        for template_path in self._get_all_template_paths():
            self._rendered_single_template(template_path)

    def _get_all_template_paths(self):
        for path in self._get_all_file_paths():
            if '.template' in path:
                yield path

    def _get_all_conf_files_paths(self):
        for path in self._get_all_file_paths():
            if not '.template' in path:
                yield path

    def _get_all_file_paths(self, path=None):
        if path is None:
            path = self._get_rendered_service_dir_path()

        for item in listdir(path):
            new_path = path + item
            if isdir(new_path):
                new_path += '/'
                yield from self._get_all_file_paths(new_path)
            elif isfile(new_path):
                yield new_path

    def _rendered_single_template(self, path):
        dir_path = NodeManager._get_dir_path_from_file_path(path)
        target_filename = NodeManager._get_filename_from_template_path(path)
        env = Environment(loader=FileSystemLoader(dir_path))
        with open(f"{dir_path}{target_filename}", 'w') as output_file:
            output_file.write(env.get_template(f'{target_filename}.template').render(self.props))

    @staticmethod
    def _get_filename_from_template_path(path):
        return NodeManager._get_filename_from_file_path(path).split('.template')[0]

    @staticmethod
    def _get_filename_from_file_path(path):
        return path.split('/')[-1]

    @staticmethod
    def _get_dir_path_from_file_path(path):
        return '/'.join(path.split('/')[:-1]) + '/'

    def _make_host_tmp_path(self):
        self.conn.run(f"mkdir -p {self.props['host_tmp_path']}{self.props['service_name']}")

    def _copy_files_to_host(self):
        for path in self._get_all_conf_files_paths():
            rel_path = self._get_rel_dir_path_from_file_path(path)
            host_dir_path = f"{self.props['host_tmp_path']}{self.props['service_name']}/{rel_path}"
            self.conn.run(f'mkdir -p {host_dir_path}')
            self.conn.put(f"{path}", remote=f"{host_dir_path}")

    def _copy_files_to_container(self):
        for path in self._get_all_conf_files_paths():

            rel_path = self._get_rel_path_from_file_path(path)
            host_file_path = f"{self.props['host_tmp_path']}{self.props['service_name']}/{rel_path}"

            rel_dir_path = self._get_rel_dir_path_from_file_path(path)
            container_dir_path = self.props['container_installation_path'] + rel_dir_path

            self.conn.run(
                f"docker exec {self.props['container_name']} mkdir -p {container_dir_path}")
            self.conn.run(
                f"docker cp {host_file_path} {self.props['container_name']}:{container_dir_path}"
            )

    def _get_rel_path_from_file_path(self, path):
        discarded_path = f"{self._get_rendered_dir_path()}{self.props['service_name']}/"
        rel_path = path.split(discarded_path)[1]
        return rel_path

    def _get_rel_dir_path_from_file_path(self, path):
        rel_path = self._get_rel_path_from_file_path(path)
        rel_dir_path = self._get_dir_path_from_file_path(rel_path)
        return '/'.join(rel_dir_path.split('/'))

    def _remove_host_tmp_path(self):
        self.conn.run(f"rm -rf {self.props['host_tmp_path']}{self.props['service_name']}")