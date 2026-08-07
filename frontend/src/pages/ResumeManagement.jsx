/**
 * Resume Management.
 *
 * Upload a resume, see every resume on file, and choose the one the rest of
 * the platform works against. Selecting a resume is the single most
 * consequential action in the product — it decides which scores, which matches
 * and which application history everything else shows — so the active card is
 * marked unmistakably rather than with a subtle highlight.
 *
 * Rename and delete are deliberately absent: the backend exposes no endpoint
 * for either (`/resumes` supports upload, list, detail, analyze and profile
 * only), and this phase is not permitted to change the API. A button that
 * looked real and did nothing would be worse than its absence.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { getResume, listResumes, uploadResume } from '../api/endpoints.js'
import { Icon } from '../components/Icon.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { useActiveResume } from '../state/ActiveResume.jsx'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Notice,
  PageHeader,
  Skeleton,
  fileKind,
  formatBytes,
  formatDate,
  humanize,
} from '../components/ui.jsx'

const STATUS_TONE = {
  uploaded: 'neutral',
  parsing: 'warn',
  parsed: 'good',
  failed: 'bad',
}

const ACCEPT =
  '.pdf,.docx,.txt,application/pdf,text/plain,' +
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

export function ResumeManagement({ navigate }) {
  const { activeResumeId, setActiveResume } = useActiveResume()
  const resumes = useAsync((signal) => listResumes({ pageSize: 100 }, signal), [])

  const fileInputRef = useRef(null)
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [uploadError, setUploadError] = useState(null)
  const [uploadResult, setUploadResult] = useState(null)

  const items = resumes.data?.items ?? []

  // `GET /resumes` returns a summary without the analysis fields, so detail is
  // fetched per resume to show whether each has been analysed. One request per
  // card is acceptable at this scale and the list is not paginated in the UI;
  // if resume counts ever grew large this should move to the list endpoint.
  const [details, setDetails] = useState({})
  // Keyed on the fetched page rather than on `items`, which is a fresh array
  // literal whenever the list has not loaded and would re-trigger the effect.
  useEffect(() => {
    let cancelled = false
    const loaded = resumes.data?.items ?? []
    if (loaded.length === 0) return undefined

    Promise.all(
      loaded.map((resume) =>
        getResume(resume.id)
          .then((detail) => [resume.id, detail])
          // A detail that fails to load costs one card's analysis line, not
          // the page: the card still renders from the summary.
          .catch(() => [resume.id, null]),
      ),
    ).then((pairs) => {
      if (!cancelled) setDetails(Object.fromEntries(pairs))
    })

    return () => {
      cancelled = true
    }
  }, [resumes.data])

  const upload = useCallback(
    async (file) => {
      if (!file) return
      setUploading(true)
      setUploadError(null)
      setUploadResult(null)
      try {
        const { data, message } = await uploadResume(file)
        setUploadResult({ ...data, message })
        // Select what was just uploaded so the next screen has a target.
        if (data?.resume) setActiveResume(data.resume)
        await resumes.run()
      } catch (error) {
        setUploadError(error)
      } finally {
        setUploading(false)
        // Allows re-selecting the same file to retry after a failure.
        if (fileInputRef.current) fileInputRef.current.value = ''
      }
    },
    [resumes, setActiveResume],
  )

  return (
    <div className="stack">
      <PageHeader
        title="Resume Management"
        lede="Upload a candidate resume and choose which one the platform searches and scores against."
        actions={
          <Button variant="ghost" icon="refresh" onClick={resumes.run} disabled={resumes.loading}>
            Refresh
          </Button>
        }
      />

      <Card title="Upload a resume" subtitle="PDF, DOCX or TXT. Validated by content, not by filename.">
        <div
          className={`dropzone ${dragging ? 'dropzone-active' : ''}`}
          role="button"
          tabIndex={0}
          onClick={() => fileInputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              fileInputRef.current?.click()
            }
          }}
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault()
            setDragging(false)
            upload(event.dataTransfer.files?.[0])
          }}
        >
          <span className="dropzone-icon">
            <Icon name={uploading ? 'clock' : 'upload'} />
          </span>
          <span className="dropzone-title">
            {uploading ? 'Uploading…' : 'Drop a resume here, or click to browse'}
          </span>
          <span className="dropzone-hint">
            PDF, DOCX or TXT · uploading a file already on record selects it instead of duplicating it
          </span>
          <input
            ref={fileInputRef}
            type="file"
            accept={ACCEPT}
            className="sr-only"
            onChange={(event) => upload(event.target.files?.[0])}
            disabled={uploading}
            aria-label="Resume file"
          />
        </div>

        {uploading && <div className="progress-rail" />}
        {uploadError && <ErrorState error={uploadError} />}

        {uploadResult && (
          <Notice tone={uploadResult.created ? 'good' : 'warn'}>
            <strong>{uploadResult.created ? 'Uploaded.' : 'Already on file.'}</strong>{' '}
            {/* The backend deduplicates by checksum and returns the existing
                record rather than storing the same bytes twice. */}
            {uploadResult.message ||
              (uploadResult.created
                ? `${uploadResult.resume?.original_filename} is now the active resume.`
                : 'This file matched a resume already uploaded, which has been selected.')}
          </Notice>
        )}
      </Card>

      <div>
        <div className="row" style={{ marginBottom: 'var(--space-4)' }}>
          <h2 className="section-heading" style={{ fontSize: 'var(--text-lg)' }}>
            Resumes on file
          </h2>
          {!resumes.loading && <Badge tone="neutral">{items.length}</Badge>}
        </div>

        {resumes.loading ? (
          <div className="grid grid-cards">
            <Skeleton variant="card" count={3} height={210} />
          </div>
        ) : resumes.error ? (
          <ErrorState error={resumes.error} onRetry={resumes.run} />
        ) : items.length === 0 ? (
          <Card>
            <EmptyState
              icon="document"
              title="No resumes uploaded yet"
              hint="Upload a PDF, DOCX or TXT file above. Everything else in the platform is scoped to the resume you select here."
            />
          </Card>
        ) : (
          <div className="grid grid-cards">
            {items.map((resume) => (
              <ResumeCard
                key={resume.id}
                resume={resume}
                detail={details[resume.id]}
                isActive={resume.id === activeResumeId}
                onSelect={() => setActiveResume(resume)}
                navigate={navigate}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function ResumeCard({ resume, detail, isActive, onSelect, navigate }) {
  const analysed = detail?.has_profile

  return (
    <article className={`resume-card ${isActive ? 'resume-card-active' : ''}`}>
      {isActive && (
        <span className="active-ribbon">
          <Badge tone="accent" dot>
            Active
          </Badge>
        </span>
      )}

      <div className="resume-card-top">
        <span className="file-icon">{fileKind(resume.content_type)}</span>
        <div style={{ minWidth: 0, paddingRight: isActive ? 60 : 0 }}>
          <h3 className="resume-name">{resume.original_filename}</h3>
          <p className="resume-meta">
            {formatBytes(resume.file_size_bytes)} · Uploaded {formatDate(resume.created_at)}
          </p>
        </div>
      </div>

      <div className="resume-card-facts">
        <div>
          <span className="resume-fact-label">Parsing</span>
          <span className="resume-fact-value">
            <Badge tone={STATUS_TONE[resume.status] ?? 'neutral'}>{humanize(resume.status)}</Badge>
          </span>
        </div>
        <div>
          <span className="resume-fact-label">Last analysis</span>
          <span className="resume-fact-value">
            {detail === undefined ? (
              <span className="skeleton" style={{ display: 'block', width: 70, height: 16 }} />
            ) : analysed ? (
              <Badge tone="good">{detail.analysis_version ?? 'Analysed'}</Badge>
            ) : (
              <Badge tone="neutral">Not analysed</Badge>
            )}
          </span>
        </div>
      </div>

      <div className="resume-card-actions">
        {isActive ? (
          <>
            <Button
              variant="primary"
              size="sm"
              icon={analysed ? 'search' : 'insight'}
              onClick={() => navigate(analysed ? 'find' : 'analysis')}
              className="btn-block"
            >
              {analysed ? 'Find matches' : 'Run analysis'}
            </Button>
            <Button variant="default" size="sm" onClick={() => navigate('analysis')}>
              Analysis
            </Button>
          </>
        ) : (
          <Button variant="default" size="sm" icon="check" onClick={onSelect} className="btn-block">
            Set as active
          </Button>
        )}
      </div>
    </article>
  )
}
