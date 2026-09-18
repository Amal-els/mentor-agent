import json
import os

with open('.hermes/desktop-attachments/Architecture final-6.excalidraw', 'r') as f:
    data = json.load(f)
os.makedirs('docs/whiteboards', exist_ok=True)
frames = {}
for elem in data['elements']:
    if elem['type'] == 'frame':
        frames[elem['id']] = {'frame': elem, 'elements': []}
for elem in data['elements']:
    if elem['frameId'] and elem['frameId'] in frames:
        frames[elem['frameId']]['elements'].append(elem)
for frame_id, frame_data in frames.items():
    frame_name = frame_data['frame'].get('name', f'frame_{frame_id}').replace(' ', '_').replace('/', '_')
    new_data = {
        'type': 'excalidraw',
        'version': 2,
        'source': 'https://excalidraw.com',
        'elements': [frame_data['frame']] + frame_data['elements'],
        'appState': data['appState'],
        'files': data['files']
    }
    safe_name = frame_name.replace('(', '').replace(')', '').replace(':', '').replace(',', '').replace('.', '').replace(' ', '_')
    filename = f'docs/whiteboards/{safe_name}.excalidraw'
    with open(filename, 'w') as f:
        json.dump(new_data, f)
    print(f"Created: {filename}")

print("Done!")