import json
with open('data/buildings.geojson') as f:
    gj = json.load(f)
feats = gj['features']

reg = [f for f in feats if f['properties']['source'] == 'toronto_heritage_register']
print('Heritage Register records enriched with descriptive names:')
for f in reg:
    p = f['properties']
    addr = p['address']
    name = p['name']
    if name != addr:
        wiki = ' [W]' if p.get('wikipedia_url') else ''
        print('  ' + addr.ljust(30) + ' -> ' + name + wiki)

has_wiki = sum(1 for f in feats if f['properties'].get('wikipedia_url'))
print('\nBuildings with Wikipedia URL: ' + str(has_wiki) + '/' + str(len(feats)))

lt = [f for f in feats if 'Little Trinity' in f['properties'].get('name','')]
for f in lt:
    p = f['properties']
    print('\n' + p['name'] + ' | ' + p['address'] + ' | ' + str(p.get('wikipedia_url','')))
