FROM python:3.11-slim

# Tesseract provides the local OCR text used for evidence verification and the
# text-only extraction route; the vision route works without it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# One command over documents/ writing one JSON per PDF to output/.
# Provide GEMINI_API_KEY and/or GROQ_API_KEY via the environment (never bake
# credentials into an image). Example:
#   docker run --rm -e GEMINI_API_KEY=... -e GROQ_API_KEY=... \
#     -v "$PWD/output:/app/output" bookable-payable
CMD ["python", "main.py", "--input", "documents", "--output", "output"]
