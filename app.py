from flask import Flask, request, render_template, send_from_directory, jsonify, url_for, redirect
import os
import pytesseract
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True
from pdf2image import convert_from_path
import re
import easyocr
import json
from rapidfuzz import fuzz
import requests

def fuzz_match(a, b, threshold=85):
    if not a or not b:
        return False
    return fuzz.ratio(a.strip(), b.strip()) >= threshold

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# OCR config
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
POPPLER_PATH = r'C:\Users\rdesimin\Downloads\Release-24.08.0-0\poppler-24.08.0\Library\bin'
reader = easyocr.Reader(['en'])

uploaded_files = {'labels': [], 'slip': None}


def convert_pdf_to_png(pdf_path):
    images = convert_from_path(pdf_path, poppler_path=POPPLER_PATH)
    image_path = pdf_path.replace(".pdf", ".png")
    images[0].save(image_path, 'PNG')
    return image_path


def extract_easyocr_text(image_path):
    results = reader.readtext(image_path)
    return [text.strip() for (_, text, conf) in results if conf > 0.5]


def crop_region_text(image_path, region):
    image = Image.open(image_path)
    cropped = image.crop(region)
    return pytesseract.image_to_string(cropped).strip()


def extract_slip_fields_from_template(image_path):
    if not os.path.exists('crop_zones.json'):
        print("⚠️ crop_zones.json not found. Please visit /crop_zones to define crop areas.")
        return {'pn': '', 'rev': '', 'lot': '', 'qty': ''}

    with open('crop_zones.json', 'r') as f:
        zones = json.load(f)

    cropped_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'cropped')
    os.makedirs(cropped_dir, exist_ok=True)

    import cv2
    import numpy as np

    image_pil = Image.open(image_path)
    actual_width, actual_height = image_pil.size

    # Assume all zones share the same display dimensions (from front-end)
    try:
        display_width = zones[0]['displayWidth']
        display_height = zones[0]['displayHeight']
    except KeyError:
        print("❌ Missing 'displayWidth' or 'displayHeight' in crop_zones.json.")
        return {'pn': '', 'rev': '', 'lot': '', 'qty': ''}

    scale_x = actual_width / display_width
    scale_y = actual_height / display_height

    def save_crop(zone_id, name_prefix):
        zone = next((z for z in zones if z['id'] == zone_id), None)
        if not zone:
            print(f"⚠️ Zone '{zone_id}' not found.")
            return ''

        x1 = int(zone['left'] * scale_x)
        y1 = int(zone['top'] * scale_y)
        x2 = int((zone['left'] + zone['width']) * scale_x)
        y2 = int((zone['top'] + zone['height']) * scale_y)

        x1, x2 = max(0, x1), min(actual_width, x2)
        y1, y2 = max(0, y1), min(actual_height, y2)

        cropped = image_pil.crop((x1, y1, x2, y2))

        # Save raw
        raw_path = os.path.join(cropped_dir, f'{name_prefix}_raw.png')
        cropped.save(raw_path)

        # Processed (grayscale + threshold)
        raw_np = np.array(cropped.convert("L"))
        _, thresh_np = cv2.threshold(raw_np, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        proc_img = Image.fromarray(thresh_np)
        proc_path = os.path.join(cropped_dir, f'{name_prefix}_proc.png')
        proc_img.save(proc_path)

        return pytesseract.image_to_string(proc_img).strip()

    # Extract and OCR each field
    part_text = save_crop('box-part', 'part')
    dwg_text = save_crop('box-dwg', 'dwg')
    shipto_text = save_crop('box-shipto', 'shipto')
    lot_text = save_crop('box-lot', 'lot')

    # Clean DWG to strip "DWG" label if present
    dwg_clean = dwg_text.replace("DWG", "").strip()
    if not dwg_clean:
        dwg_clean = ''

    # Clean LOT
    lot_clean = lot_text.strip()
    if lot_clean.upper() == "LOT ID" or not lot_clean:
        lot_clean = ''

    return {
        'pn': re.search(r'\d{5,7}', part_text).group(0) if re.search(r'\d{5,7}', part_text) else '',
        'rev': dwg_clean,
        'lot': lot_clean,
        'shipto': shipto_text.strip(),
    }


def extract_label_fields(lines):
    pn = rev = lot = qty = ''
    for i, line in enumerate(lines):
        upper = line.upper()
        if 'PN' in upper and not pn:
            match = re.search(r'PN[:\s\-]*([A-Z0-9\-]+)', upper)
            if match:
                pn = match.group(1)
        if 'REV' in upper and not rev:
            match = re.search(r'REV[:\s\-]*([A-Z0-9]{1,4})', upper)
            if match:
                rev = match.group(1)
        if 'LOT' in upper and not lot:
            if upper.strip() == 'LOT' and i + 1 < len(lines):
                lot_line = lines[i + 1]
            else:
                lot_line = line
            match = re.search(r'([A-Z0-9\-]+)', lot_line)
            if match:
                lot = match.group(1)
        if 'QTY' in upper and not qty:
            match = re.search(r'QTY[:\s\-]*([0-9]+)', upper)
            if match:
                qty = match.group(1)
    return {'pn': pn, 'rev': rev, 'lot': lot, 'qty': qty}

@app.route('/', methods=['GET', 'POST'])
def index():
    label_data = slip_data = match_result = {}
    slip_filename = None

    if request.method == 'POST':
        slip_file = request.files.get('slip_file')
        label_files = request.files.getlist('label_image')
        uploaded_files['labels'] = []

        for label_file in label_files:
            label_path = os.path.join(app.config['UPLOAD_FOLDER'], label_file.filename)
            label_file.save(label_path)
            if label_file.filename.endswith(".pdf"):
                label_path = convert_pdf_to_png(label_path)
            uploaded_files['labels'].append(label_path)

        if slip_file:
            slip_filename = slip_file.filename
            slip_path = os.path.join(app.config['UPLOAD_FOLDER'], slip_filename)
            slip_file.save(slip_path)
            if slip_filename.endswith(".pdf"):
                slip_path = convert_pdf_to_png(slip_path)
                slip_filename = os.path.basename(slip_path)
            uploaded_files['slip'] = slip_path

        return redirect(url_for('crop_zones'))

    return render_template("canvas_ocr.html",
                           slip_filename=slip_filename,
                           label=label_data,
                           slip=slip_data,
                           match=match_result)


@app.route('/verify_after_crop')
def verify_after_crop():
    labels = uploaded_files.get('labels', [])
    slip_path = uploaded_files.get('slip')

    if not labels or not slip_path:
        return "❌ Missing uploaded label(s) or slip.", 400

    slip_data = extract_slip_fields_from_template(slip_path)

    # 🧼 Default formatting
    if not slip_data.get('lot'):
        slip_data['lot'] = 'NOT LOT CONTROLLED'
    if not slip_data.get('rev'):
        slip_data['rev'] = 'N/A'

    from concurrent.futures import ThreadPoolExecutor

    def process_label(label_path):
        label_lines = extract_easyocr_text(label_path)
        label_data = extract_label_fields(label_lines)
        match_result = {
            'pn': fuzz_match(label_data.get('pn'), slip_data.get('pn')),
            'rev': fuzz_match(label_data.get('rev'), slip_data.get('rev')),
            'lot': fuzz_match(label_data.get('lot'), slip_data.get('lot')),
        }
        return {
            'filename': os.path.basename(label_path),
            'label': label_data,
            'match': match_result
        }

    # 🧵 Multithreaded execution
    with ThreadPoolExecutor() as executor:
        results = list(executor.map(process_label, labels))

    return render_template("canvas_ocr.html",
                           slip_filename=os.path.basename(slip_path),
                           slip=slip_data,
                           results=results)


@app.route('/uploads/<path:filename>')  # Accept subfolders like 'cropped/part_raw.png'
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/crop_zones')
def crop_zones():
    slip_path = uploaded_files.get('slip')
    if not slip_path or not os.path.exists(slip_path):
        return "❌ No slip file available. Please upload a packing slip first.", 400
    slip_filename = os.path.basename(slip_path)
    return render_template('crop_interface.html', slip_filename=slip_filename)



@app.route('/save_crop_zones', methods=['POST'])
def save_crop_zones():
    data = request.get_json()
    with open('crop_zones.json', 'w') as f:
        json.dump(data['zones'], f)

    return jsonify({'redirect': url_for('verify_after_crop')})

@app.route('/api/verify', methods=['POST'])
def api_verify():
    if 'slip' not in request.files or 'labels' not in request.files:
        return jsonify({'error': 'Missing slip or labels in form-data'}), 400

    # Save and process slip
    slip_file = request.files['slip']
    slip_path = os.path.join(app.config['UPLOAD_FOLDER'], slip_file.filename)
    slip_file.save(slip_path)
    if slip_path.endswith('.pdf'):
        slip_path = convert_pdf_to_png(slip_path)

    slip_data = extract_slip_fields_from_template(slip_path)
    if not slip_data.get('lot'):
        slip_data['lot'] = 'NOT LOT CONTROLLED'
    if not slip_data.get('rev'):
        slip_data['rev'] = 'N/A'

    # Handle single or multiple labels
    label_files = request.files.getlist('labels')
    results = []

    for label_file in label_files:
        label_path = os.path.join(app.config['UPLOAD_FOLDER'], label_file.filename)
        label_file.save(label_path)
        if label_file.filename.endswith(".pdf"):
            label_path = convert_pdf_to_png(label_path)

        label_lines = extract_easyocr_text(label_path)
        label_data = extract_label_fields(label_lines)
        match_result = {
            'pn': fuzz_match(label_data.get('pn'), slip_data.get('pn')),
            'rev': fuzz_match(label_data.get('rev'), slip_data.get('rev')),
            'lot': fuzz_match(label_data.get('lot'), slip_data.get('lot')),
        }

        results.append({
            'filename': label_file.filename,
            'label_data': label_data,
            'match_result': match_result
        })

    return jsonify({
        'slip_data': slip_data,
        'results': results
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)


