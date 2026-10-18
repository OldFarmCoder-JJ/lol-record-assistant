"""Read Mayhem's public website data; never substitute ordinary ARAM stats."""
import json
import re
from html.parser import HTMLParser


class MayhemDataError(ValueError):
    pass


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.inside = tag == 'script'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


class MayhemPage:
    """Decode JSON carried by Next's streamed page without executing JavaScript."""
    def __init__(self, html):
        parser = _Scripts()
        parser.feed(html)
        chunks = []
        for script in parser.parts:
            for match in re.finditer(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', script):
                chunks.append(json.loads(match.group(1)))
        self.records = {}
        for line in ''.join(chunks).splitlines():
            key, sep, value = line.partition(':')
            if not sep:
                continue
            try:
                self.records[key] = json.loads(value)
            except ValueError:
                continue  # Module references and length-prefixed text are not JSON.
        if not self.records:
            raise MayhemDataError('OP.GG 海克斯大乱斗页面结构已变化或请求被拦截，请稍后重试。')

    def resolve(self, value):
        if not isinstance(value, str) or not value.startswith('$'):
            return value
        key, *path = value[1:].split(':')
        if key.startswith('L'):
            key = key[1:]
        node = self.records.get(key)
        if node is None:
            return value
        try:
            for part in path:
                if part == 'props' and isinstance(node, list) and len(node) == 4 and node[0] == '$':
                    node = node[3]
                elif isinstance(node, list):
                    node = node[int(part)]
                else:
                    node = node[part]
            return node
        except (KeyError, IndexError, TypeError, ValueError):
            return value

    def nodes(self, root=None):
        stack = list(reversed(list(self.records.values()))) if root is None else [root]
        seen = set()
        while stack:
            node = self.resolve(stack.pop())
            if not isinstance(node, (dict, list)) or id(node) in seen:
                continue
            seen.add(id(node))
            yield node
            if isinstance(node, dict):
                stack.extend(reversed([v for k, v in node.items() if k not in ('messages', 'catalog')]))
            else:
                stack.extend(reversed(node))

    def champions(self):
        for node in self.nodes():
            if isinstance(node, dict) and node.get('isAramMayhem') is True:
                champions = node.get('champions')
                query = node.get('query') or {}
                if champions and all('champion_id' in c and 'key' in c for c in champions):
                    return champions, query.get('patch') or (node.get('versions') or [''])[0]
        raise MayhemDataError('OP.GG 未返回海克斯大乱斗英雄榜，未使用普通大乱斗替代。')

    def augments(self):
        for node in self.nodes():
            if isinstance(node, dict):
                rows = node.get('data')
                if isinstance(rows, list) and rows and all(
                        isinstance(row, dict) and {'id', 'tier', 'performance', 'popular'} <= row.keys()
                        for row in rows) and any('rarity' in row for row in rows):
                    return rows
        raise MayhemDataError('OP.GG 未返回该英雄的海克斯强化符文推荐。')

    def champion_key(self):
        for node in self.nodes():
            if isinstance(node, dict) and isinstance(node.get('championKey'), str):
                return node['championKey']
        raise MayhemDataError('OP.GG 未返回当前英雄标识，无法确认强化推荐所属英雄。')

    def recommendations(self):
        result = {'starter_items': [], 'core_items': [], 'boots': [], 'spells': [], 'skillOrder': []}
        seen = set()
        for node in self.nodes():
            if not isinstance(node, list) or len(node) != 4 or node[0] != '$':
                continue
            tag, key, props = node[1:]
            if not isinstance(props, dict):
                continue
            if isinstance(key, str):
                group = next((g for g in result if key.startswith(g+'_')), None)
                if group and tag == 'tr':
                    ids = []
                    for child in self.nodes(props):
                        if isinstance(child, dict) and child.get('metaType') == 'item':
                            item = child.get('metaId')
                            if item not in ids:
                                ids.append(item)
                    token = (group, tuple(ids))
                    if ids and token not in seen:
                        result[group].append(ids)
                        seen.add(token)
            if isinstance(key, str) and key.startswith('spell_table_'):
                ids = []
                for child in self.nodes(props):
                    if isinstance(child, dict) and child.get('metaType') == 'spell':
                        item = child.get('metaId')
                        if item not in ids:
                            ids.append(item)
                if ids and ids not in result['spells']:
                    result['spells'].append(ids)
            if tag == 'span' and isinstance(key, str) and key.isdigit():
                for child in self.nodes(props):
                    if isinstance(child, list) and len(child) == 4 and child[1] == 'strong':
                        skill = child[3].get('children')
                        if skill in ('Q', 'W', 'E', 'R'):
                            token = ('skill', key)
                            if token not in seen:
                                result['skillOrder'].append((int(key), skill))
                                seen.add(token)
        result['skillOrder'] = [skill for _, skill in sorted(result['skillOrder'])]
        if not result['core_items']:
            raise MayhemDataError('OP.GG 未返回海克斯出装推荐，页面结构可能已变化。')
        return result
