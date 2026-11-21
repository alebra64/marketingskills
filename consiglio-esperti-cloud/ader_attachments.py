"""Allegati riservati per la pratica AdER: caricamento autenticato e revisione immagini."""
from flask import request, jsonify, Response
import hashlib, base64, json, os, threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

CASE='ader-pratica-principale'
MAX_IMAGE=4*1024*1024
TYPES={'image/jpeg':(b'\xff\xd8\xff',),'image/png':(b'\x89PNG\r\n\x1a\n',)}
REVIEW_LOCK=threading.Lock()

def register(app, dbconn, require_auth, safe_post, fetch_practice, update_practice):
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

    @app.post('/api/cases/<pid>/review-photos')
    @require_auth
    @safe_post
    def review_ader_photos(pid):
        if pid!=CASE:return jsonify(error='Fascicolo non trovato'),404
        if not request.is_json or request.json.get('consent') is not True:
            return jsonify(error='Serve il consenso esplicito all’invio di quesito e fotografie a Gemini'),400
        key=os.getenv('GEMINI_API_KEY','').strip()
        if not key:return jsonify(error='Chiave Gemini non configurata'),409
        if not REVIEW_LOCK.acquire(blocking=False):return jsonify(error='Lettura delle fotografie già in corso'),409
        try:
            with dbconn() as cn:
                rows=cn.execute('SELECT slot,content_type,body FROM ader_document_images WHERE case_id=%s ORDER BY slot',(pid,)).fetchall()
            if len(rows)!=3 or [r[0] for r in rows]!=[1,2,3]:
                return jsonify(error='Carica e verifica tutte e tre le fotografie prima di avviare la lettura'),409
            p=fetch_practice(pid)
            if not p:return jsonify(error='Fascicolo non trovato'),404
            parts=[{'text':(
                "Esamina le tre foto dell’atto AdER nell’ordine. Fornisci una lettura essenziale e precisa: "
                "trascrivi le parti leggibili utili a identificare atto, date, importi, destinatari, "
                "riferimenti normativi e istruzioni; indica a quale foto appartiene ogni dato. Segnala "
                "le parti illeggibili o dubbie senza ricostruirle. Confronta i dati con il quesito e "
                "separa corrispondenze, differenze e verifiche necessarie. Non dare conclusioni "
                "giuridiche definitive. Rispondi in modo conciso.\n\nQUESITO:\n"
                +str(p.get('quesito',''))[:8000]
            )}]
            for slot,ctype,body in rows:
                parts.append({'inlineData':{'mimeType':ctype,'data':base64.b64encode(bytes(body)).decode('ascii')}})
            payload={'contents':[{'role':'user','parts':parts}],
                'generationConfig':{'maxOutputTokens':4000,'temperature':0.1}}
            models=('gemini-3.8-flash','gemini-3.5-flash-lite')
            output=''
            used_model=''
            last_error=''
            for model_name in models:
                req=Request('https://generativelanguage.googleapis.com/v1beta/models/'+
                    model_name+':streamGenerateContent?alt=sse',
                    data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
                    headers={'Content-Type':'application/json','Accept':'text/event-stream','x-goog-api-key':key},method='POST')
                chunks=[]
                try:
                    with urlopen(req,timeout=120) as resp:
                        for rawline in resp:
                            line=rawline.decode('utf-8','replace').strip()
                            if not line.startswith('data:'):continue
                            data=line[5:].strip()
                            if not data or data=='[DONE]':continue
                            try:event=json.loads(data)
                            except ValueError:continue
                            for candidate in event.get('candidates',[]):
                                for part in candidate.get('content',{}).get('parts',[]):
                                    if isinstance(part,dict) and isinstance(part.get('text'),str):
                                        chunks.append(part['text'])
                except HTTPError as exc:
                    detail=exc.read(500).decode('utf-8','replace')
                    last_error='HTTP '+str(exc.code)+': '+detail[:180]
                    if exc.code in (429,500,502,503,504) and model_name!=models[-1]:
                        continue
                    return jsonify(error='Gemini ha rifiutato la lettura ('+last_error+')'),502
                except (URLError,TimeoutError,OSError) as exc:
                    last_error=type(exc).__name__
                    if model_name!=models[-1]:continue
                    return jsonify(error='Errore di collegamento a Gemini: '+last_error),502
                output=''.join(chunks)
                if output.strip():
                    used_model=model_name
                    break
                last_error='risposta vuota'
            if not output.strip():
                return jsonify(error='Gemini non ha completato la lettura. Ultimo esito: '+last_error),502
            if not output.strip():
                return jsonify(error='Gemini non ha restituito una lettura. Le foto restano conservate nel fascicolo.'),502
            stages=dict(p.get('stages') or {})
            previous=stages.get('documenti_originali',{})
            stage={'name':'Lettura delle tre fotografie originali AdER','provider':'gemini',
                'model':used_model,'output':output.strip(),
                'stato':'COMPLETATO','nota':'Foto e quesito inviati a Gemini dopo consenso esplicito'}
            if previous.get('output'):stage['output_precedente']=previous['output']
            stages['documenti_originali']=stage
            old_final=(p.get('final') or '').strip()
            marker='## Lettura delle fotografie originali AdER'
            if marker in old_final:old_final=old_final.split(marker,1)[0].rstrip()
            final=(old_final+'\n\n'+marker+'\n\n'+output.strip()).strip()
            update_practice(pid,stages=stages,final=final)
            return jsonify(ok=True,status='COMPLETATO',provider='Gemini')
        finally:
            REVIEW_LOCK.release()
