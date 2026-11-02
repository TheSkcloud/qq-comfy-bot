"""Validate API graph references and installed model choices before GPU submission."""


def validate_graph(graph, info):
    if not isinstance(graph, dict) or not graph:
        raise ValueError('工作流为空或不是 API 格式')
    dependencies = {}
    for nid, node in graph.items():
        if not isinstance(node, dict) or node.get('class_type') not in info:
            raise ValueError(f'工作流缺少节点 {nid}/{node.get("class_type") if isinstance(node,dict) else "unknown"}')
        inputs = node.get('inputs', {})
        schema = info[node['class_type']].get('input', {})
        if not isinstance(inputs, dict):
            raise ValueError(f'节点 {nid} inputs 不是对象')
        for name in schema.get('required', {}):
            if name not in inputs:
                raise ValueError(f'节点 {nid} 缺少必填参数 {name}')
        allowed = {**schema.get('required', {}), **schema.get('optional', {})}
        dependencies[nid] = []
        for name, value in inputs.items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int:
                source, slot = value
                if source not in graph or slot < 0:
                    raise ValueError(f'节点 {nid}/{name} 引用了不存在的输出 {source}:{slot}')
                source_node = graph[source]
                source_info = info.get(source_node.get('class_type'), {}) if isinstance(source_node,dict) else {}
                outputs = source_info.get('output')
                if outputs is not None and slot >= len(outputs):
                    raise ValueError(f'节点 {nid}/{name} 输出序号越界 {source}:{slot}')
                dependencies[nid].append(source)
            elif name in allowed:
                spec = allowed[name]
                if spec and isinstance(spec[0], list) and value not in spec[0]:
                    raise ValueError(f'节点 {nid}/{name} 的模型文件名或选项不可用：{value}')
    visiting, done = set(), set()
    def visit(nid):
        if nid in visiting:
            raise ValueError(f'工作流存在循环依赖：{nid}')
        if nid in done:
            return
        visiting.add(nid)
        for source in dependencies[nid]:
            visit(source)
        visiting.remove(nid)
        done.add(nid)
    for nid in graph:
        visit(nid)
