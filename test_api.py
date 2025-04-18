import requests

url = 'http://localhost:5000/api/verify'

files = [
    ('slip', open('packing_slip.pdf', 'rb')),  # your slip
    ('labels', open('label1.png', 'rb')),      # first label
    ('labels', open('label2.png', 'rb'))       # second label (optional)
]

response = requests.post(url, files=files)

if response.ok:
    data = response.json()
    print(f"Slip Data:\n{data['slip_data']}\n")
    for r in data['results']:
        print(f"File: {r['filename']}")
        print(f"  ✅ PN Match:  {r['match_result']['pn']}")
        print(f"  ✅ REV Match: {r['match_result']['rev']}")
        print(f"  ✅ LOT Match: {r['match_result']['lot']}")
        print(f"  Extracted OCR: {r['label_data']}")
        print("-" * 40)
else:
    print("❌ Error:", response.text)
