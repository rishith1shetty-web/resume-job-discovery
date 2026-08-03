import { useRef, useState } from 'react'

import { listResumes, uploadResume } from '../api/endpoints.js'
import { useAsync } from '../hooks/useAsync.js'
import { useActiveResume } from '../state/ActiveResume.jsx'
import {
  AsyncBlock,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Table,
  formatBytes,
  formatDateTime,
  humanize,
  orDash,
} from '../components/ui.jsx'

const STATUS_TONE = {
  uploaded: 'neutral',
  parsing: 'warn',
  parsed: 'good',
  failed: 'bad',
}

/** Extension from the content type the API returned, never guessed. */
const CONTENT_TYPE_LABEL = {
  'application/pdf': 'PDF',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
  'text/plain': 'TXT',
}

export function Resumes() {
  const { activeResumeId, setActiveResume } = useActiveResume()
  const resumes = useAsync((signal) => listResumes({ pageSize: 100 }, signal), [])

  const fileInputRef = useRef(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState(null)
  const [uploadResult, setUploadResult] = useState(null)

  async function handleUpload(event) {
    const file = event.target.files?.[0]
    if (!file) return

    setUploading(true)
    setUploadError(null)
    setUploadResult(null)

    try {
      const { data, message } = await uploadResume(file)
      setUploadResult({ ...data, message })
      // Select what was just uploaded, so the next sections have a target.
      if (data?.resume) setActiveResume(data.resume)
      await resumes.run()
    } catch (error) {
      setUploadError(error)
    } finally {
      setUploading(false)
      // Allows re-selecting the same file to retry after a failure.
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const items = resumes.data?.items ?? []

  return (
    <div className="stack">
      <Card
        title="Upload a resume"
        subtitle="PDF, DOCX or TXT. The file is validated by its magic bytes, not its name."
      >
        <div className="upload-row">
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,.docx,.txt,application/pdf,text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            onChange={handleUpload}
            disabled={uploading}
            aria-label="Resume file"
          />
          {uploading && <span className="muted">Uploading…</span>}
        </div>

        {uploadError && <ErrorState error={uploadError} />}

        {uploadResult && (
          <div
            className={`notice ${uploadResult.created ? 'notice-good' : 'notice-warn'}`}
          >
            <strong>
              {uploadResult.created ? 'Uploaded.' : 'Already uploaded.'}
            </strong>{' '}
            {/* The backend deduplicates by checksum and returns the existing
                record rather than storing the same bytes twice. */}
            {uploadResult.message ||
              (uploadResult.created
                ? `${uploadResult.resume?.original_filename} is now the active resume.`
                : 'This file matched an existing resume, which has been selected.')}
            {!uploadResult.created && uploadResult.duplicate_of && (
              <div className="muted mono small">
                Existing resume id: {uploadResult.duplicate_of}
              </div>
            )}
          </div>
        )}
      </Card>

      <Card
        title="Resumes"
        subtitle="Select one to make it the target for every other section."
        actions={
          <Button variant="ghost" onClick={resumes.run} disabled={resumes.loading}>
            Refresh
          </Button>
        }
      >
        <AsyncBlock
          loading={resumes.loading}
          error={resumes.error}
          data={resumes.data}
          onRetry={resumes.run}
          isEmpty={(data) => (data.items ?? []).length === 0}
          empty={
            <EmptyState
              title="No resumes uploaded yet."
              hint="Upload a PDF, DOCX or TXT file above to begin."
            />
          }
        >
          <Table
            columns={['', 'Filename', 'Type', 'Size', 'Status', 'Uploaded', 'Active']}
            rows={items}
            renderRow={(resume) => {
              const selected = resume.id === activeResumeId
              return (
                <tr key={resume.id} className={selected ? 'row-selected' : undefined}>
                  <td>
                    <input
                      type="radio"
                      name="active-resume"
                      checked={selected}
                      onChange={() => setActiveResume(resume)}
                      aria-label={`Select ${resume.original_filename}`}
                    />
                  </td>
                  <td>
                    <span className="strong">{resume.original_filename}</span>
                    <div className="muted mono small">{resume.id}</div>
                  </td>
                  <td>
                    {orDash(
                      CONTENT_TYPE_LABEL[resume.content_type] ?? resume.content_type,
                    )}
                  </td>
                  <td>{orDash(formatBytes(resume.file_size_bytes))}</td>
                  <td>
                    <Badge tone={STATUS_TONE[resume.status] ?? 'neutral'}>
                      {humanize(resume.status)}
                    </Badge>
                  </td>
                  <td>{orDash(formatDateTime(resume.created_at))}</td>
                  <td>
                    {/* `is_active` is the backend's own flag and is distinct
                        from the resume selected in this dashboard. */}
                    {resume.is_active ? <Badge tone="good">Yes</Badge> : orDash(null)}
                  </td>
                </tr>
              )
            }}
          />

          {resumes.data?.pagination && (
            <p className="muted small">
              Showing {items.length} of {resumes.data.pagination.total}
            </p>
          )}
        </AsyncBlock>
      </Card>
    </div>
  )
}
