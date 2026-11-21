"""Allegati riservati per la pratica AdER: caricamento autenticato e persistenza nel database."""
from flask import request, jsonify, Response, session
import hashlib

CASE='ader-pratica-principale'
MAX_IMAGE=4*1024*1024
TYPES={'image/jpeg':(b'\xff\xd8\xff',),'image/png':(b'\x89PNG\r\n\x1a\n',)}

def register(app, dbconn, require_auth, safe_post):
    with dbconn() as cn:
        cn.execute("""CREATE TABLE IF NOT EXISTS ader_document_images (
          case_id TEXT NOT NULL, slot SMALLINT NOT NULL CHECK(slot BETWEEN 1 AND 3),
          filename TEXT NOT NULL, content_type TEXT NOT NULL, sha256 TEXT NOT NULL,
          body BYTEA NOT NULL, uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (case_id,slot))""")

    @app.get('/api/cases/<pid>/allegati')
    @require_auth
    def list_ader_images(pid):
        if pid!=CASE:return jsonify(error='Fascicolo non trovato'),404
        with dbconn() as cn:
            rows=cn.execute('SELECT slot,filename,sha256,octet_length(body),uploaded_at FROM ader_document_images WHERE case_id=%s ORDER BY slot',(pid,)).fetchall()
        return jsonify(images=[dict(slot=x[0],filename=x[1],sha256=x[2],bytes=x[3],uploaded_at=x[4].isoformat()) for x in rows],count=len(rows))

    @app.post('/api/cases/<pid>/allegati/<int:slot>')
    @require_auth
    @safe_post
    def upload_ader_image(pid,slot):
        if pid!=CASE or slot not in (1,2,3):return jsonify(error='Fascicolo o numero foto non valido'),404
        f=request.files.get('file')
        if not f:return jsonify(error='Selezionare una fotografia'),400
        ctype=f.mimetype.lower()
        if ctype not in TYPES:return jsonify(error='Sono ammesse soltanto immagini JPEG o PNG'),415
        raw=f.stream.read(MAX_IMAGE+1)
        if not raw or len(raw)>MAX_IMAGE:return jsonify(error='Foto vuota o oltre il limite di 4 MB'),413
        if not any(raw.startswith(magic) for magic in TYPES[ctype]):return jsonify(error='Contenuto immagine non valido'),415
        digest=hashlib.sha256(raw).hexdigest()
        name=('Foto originale AdER '+str(slot)+('.png' if ctype=='image/png' else '.jpg'))
        with dbconn() as cn:
            cn.execute("""INSERT INTO ader_document_images(case_id,slot,filename,content_type,sha256,body)
              VALUES(%s,%s,%s,%s,%s,%s)
              ON CONFLICT(case_id,slot) DO UPDATE SET filename=EXCLUDED.filename,
              content_type=EXCLUDED.content_type,sha256=EXCLUDED.sha256,
              body=EXCLUDED.body,uploaded_at=now()""",
              (pid,slot,name,ctype,digest,raw))
        return jsonify(ok=True,slot=slot,sha256=digest,bytes=len(raw))

    @app.get('/api/cases/<pid>/allegati/<int:slot>')
    @require_auth
    def view_ader_image(pid,slot):
        if pid!=CASE or slot not in (1,2,3):return jsonify(error='Fascicolo o numero foto non valido'),404
        with dbconn() as cn:
            row=cn.execute('SELECT content_type,body FROM ader_document_images WHERE case_id=%s AND slot=%s',(pid,slot)).fetchone()
        if not row:return jsonify(error='Fotografia non presente'),404
        return Response(bytes(row[1]),mimetype=row[0],headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
