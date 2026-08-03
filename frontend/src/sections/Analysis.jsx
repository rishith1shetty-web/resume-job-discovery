import { useState } from 'react'

import { analyzeResume, getProfile, updateProfile } from '../api/endpoints.js'
import { ApiError } from '../api/client.js'
import { useAsync } from '../hooks/useAsync.js'
import { useActiveResume } from '../state/ActiveResume.jsx'
import {
  Badge,
  Button,
  Card,
  ConfidenceBar,
  EmptyState,
  ErrorState,
  Field,
  Spinner,
  Table,
  formatDateTime,
  humanize,
  orDash,
} from '../components/ui.jsx'

export function Analysis() {
  const { activeResumeId, activeResume } = useActiveResume()

  const profile = useAsync(
    (signal) => (activeResumeId ? getProfile(activeResumeId, signal) : Promise.resolve(null)),
    [activeResumeId],
    { immediate: Boolean(activeResumeId) },
  )

  const [analyzing, setAnalyzing] = useState(false)
  const [analyzeError, setAnalyzeError] = useState(null)
  const [analyzeMessage, setAnalyzeMessage] = useState(null)

  async function runAnalysis() {
    if (!activeResumeId) return
    setAnalyzing(true)
    setAnalyzeError(null)
    setAnalyzeMessage(null)
    try {
      const { data, message } = await analyzeResume(activeResumeId)
      profile.setData(data)
      setAnalyzeMessage(message)
    } catch (error) {
      setAnalyzeError(error)
    } finally {
      setAnalyzing(false)
    }
  }

  if (!activeResumeId) {
    return (
      <Card title="Resume analysis">
        <EmptyState
          title="No resume selected."
          hint="Choose one in Resume Management first — analysis is always scoped to a single resume."
        />
      </Card>
    )
  }

  // A resume that has never been analysed returns 404 from GET /profile. That
  // is an expected state here, not a failure to shout about.
  const notAnalyzedYet =
    profile.error instanceof ApiError && profile.error.status === 404

  return (
    <div className="stack">
      <Card
        title="Resume analysis"
        subtitle={activeResume?.original_filename ?? activeResumeId}
        actions={
          <Button onClick={runAnalysis} disabled={analyzing}>
            {analyzing ? 'Analyzing…' : profile.data ? 'Re-analyze' : 'Analyze'}
          </Button>
        }
      >
        <p className="muted small">
          Deterministic extraction — no LLM, no network. Re-analyzing creates a new
          profile version; the previous one is retained.
        </p>

        {analyzeMessage && <p className="notice notice-warn">{analyzeMessage}</p>}
        {analyzeError && <ErrorState error={analyzeError} />}
        {analyzing && <Spinner label="Running analysis…" />}

        {!analyzing && profile.loading && <Spinner label="Loading profile…" />}

        {!analyzing && !profile.loading && notAnalyzedYet && (
          <EmptyState
            title="This resume has not been analyzed yet."
            hint="Run the analysis to detect one primary domain and one target role."
          />
        )}

        {!analyzing && !profile.loading && profile.error && !notAnalyzedYet && (
          <ErrorState error={profile.error} onRetry={profile.run} />
        )}

        {!analyzing && profile.data && <ProfileView profile={profile.data} />}
      </Card>

      {profile.data && (
        <ProfileCorrection
          resumeId={activeResumeId}
          profile={profile.data}
          onUpdated={(updated) => profile.setData(updated)}
        />
      )}
    </div>
  )
}

function ProfileView({ profile }) {
  const skills = profile.skills ?? []
  const byKind = (kind) => skills.filter((skill) => skill.kind === kind)

  return (
    <div className="stack">
      <div className="detect-grid">
        <DetectionCard label="Primary domain" detection={profile.domain} />
        <DetectionCard label="Target role" detection={profile.target_role} />
      </div>

      <div className="field-grid">
        <Field label="Effective domain" hint="Confirmed value when set, else detected">
          {orDash(humanize(profile.effective_domain))}
        </Field>
        <Field label="Effective target role">
          {orDash(humanize(profile.effective_target_role))}
        </Field>
        <Field label="Seniority">{orDash(humanize(profile.seniority))}</Field>
        <Field label="Seniority confidence">
          <ConfidenceBar value={profile.seniority_confidence} />
        </Field>
        <Field
          label="Years of experience"
          hint={profile.years_experience_source ? `from ${humanize(profile.years_experience_source)}` : undefined}
        >
          {/* Null means the resume stated nothing verifiable. Never render 0. */}
          {profile.years_experience == null ? (
            <span className="muted">Not stated</span>
          ) : (
            profile.years_experience
          )}
        </Field>
        <Field label="Analysis version">{orDash(profile.analysis_version)}</Field>
        <Field label="Analyzed at">{orDash(formatDateTime(profile.analyzed_at))}</Field>
        <Field label="Sections found">
          {(profile.sections_found ?? []).length > 0
            ? profile.sections_found.map(humanize).join(', ')
            : orDash(null)}
        </Field>
      </div>

      {(profile.warnings ?? []).length > 0 && (
        <div className="notice notice-warn">
          <strong>Warnings</strong>
          <ul className="tight-list">
            {profile.warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      <Group title="Same-role title variations" count={(profile.role_variations ?? []).length}>
        <p className="muted small">
          Only variations of the detected role. Unrelated careers are never suggested.
        </p>
        <div className="chip-row">
          {(profile.role_variations ?? []).map((variation) => (
            <Badge key={variation} tone="accent">
              {variation}
            </Badge>
          ))}
        </div>
      </Group>

      <Group title="Skills" count={byKind('skill').length}>
        <SkillChips skills={byKind('skill')} />
      </Group>

      <Group title="Technologies" count={byKind('technology').length}>
        <SkillChips skills={byKind('technology')} />
      </Group>

      <Group title="Tools" count={byKind('tool').length}>
        <SkillChips skills={byKind('tool')} />
      </Group>

      <Group title="Titles" count={(profile.titles ?? []).length}>
        <Table
          columns={['Title', 'Normalized', 'Role key', 'Section', 'Most recent']}
          rows={profile.titles ?? []}
          renderRow={(title, index) => (
            <tr key={`${title.raw}-${index}`}>
              <td>{title.raw}</td>
              <td className="mono small">{title.normalized}</td>
              <td className="mono small">{orDash(title.role_key)}</td>
              <td>{orDash(humanize(title.section))}</td>
              <td>{title.is_most_recent ? <Badge tone="good">Yes</Badge> : orDash(null)}</td>
            </tr>
          )}
        />
      </Group>

      <Group title="Education" count={(profile.education ?? []).length}>
        <Table
          columns={['Raw', 'Level', 'Field', 'Institution', 'Year']}
          rows={profile.education ?? []}
          renderRow={(entry, index) => (
            <tr key={index}>
              <td>{entry.raw}</td>
              <td>{orDash(humanize(entry.degree_level))}</td>
              <td>{orDash(entry.field_of_study)}</td>
              <td>{orDash(entry.institution)}</td>
              <td>{orDash(entry.year)}</td>
            </tr>
          )}
        />
      </Group>

      <Group title="Certifications" count={(profile.certifications ?? []).length}>
        <Table
          columns={['Name', 'Raw', 'Year']}
          rows={profile.certifications ?? []}
          renderRow={(entry, index) => (
            <tr key={index}>
              <td>{entry.name}</td>
              <td className="muted">{entry.raw}</td>
              <td>{orDash(entry.year)}</td>
            </tr>
          )}
        />
      </Group>

      <Group title="Responsibilities" count={(profile.responsibilities ?? []).length}>
        <ul className="tight-list">
          {(profile.responsibilities ?? []).map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      </Group>

      <Group title="Seniority evidence" count={(profile.seniority_evidence ?? []).length}>
        <EvidenceTable evidence={profile.seniority_evidence} />
      </Group>
    </div>
  )
}

function DetectionCard({ label, detection }) {
  const value = detection?.display || detection?.key

  return (
    <div className="detect-card">
      <span className="detect-label">{label}</span>
      <span className="detect-value">
        {value ?? <span className="muted">Not detected</span>}
      </span>
      {detection?.key && <span className="mono small muted">{detection.key}</span>}
      <ConfidenceBar value={detection?.confidence} />
      {(detection?.evidence ?? []).length > 0 && (
        <details className="evidence-details">
          <summary>Evidence ({detection.evidence.length})</summary>
          <EvidenceTable evidence={detection.evidence} />
        </details>
      )}
    </div>
  )
}

function EvidenceTable({ evidence }) {
  if (!evidence || evidence.length === 0) {
    return <EmptyState title="No evidence recorded." />
  }
  return (
    <Table
      columns={['Kind', 'Value', 'Section', 'Line', 'Weight', 'Snippet']}
      rows={evidence}
      renderRow={(item, index) => (
        <tr key={index}>
          <td className="mono small">{item.kind}</td>
          <td>{item.value}</td>
          <td>{orDash(humanize(item.section))}</td>
          <td>{orDash(item.line_number)}</td>
          <td>{item.weight}</td>
          {/* A short snippet the backend chose to expose, not the resume text. */}
          <td className="muted small">{orDash(item.snippet)}</td>
        </tr>
      )}
    />
  )
}

function SkillChips({ skills }) {
  if (!skills || skills.length === 0) {
    return <EmptyState title="None extracted." />
  }
  return (
    <div className="chip-row">
      {skills.map((skill) => (
        <Badge
          key={skill.skill_key}
          tone="neutral"
          title={`${skill.skill_key} · ${skill.occurrences}× · confidence ${Math.round(
            (skill.confidence ?? 0) * 100,
          )}%`}
        >
          {skill.display}
          <span className="chip-count">{skill.occurrences}</span>
        </Badge>
      ))}
    </div>
  )
}

function Group({ title, count, children }) {
  return (
    <details className="group" open={count > 0}>
      <summary>
        <span className="section-heading">{title}</span>
        <Badge tone="neutral">{count}</Badge>
      </summary>
      <div className="group-body">{children}</div>
    </details>
  )
}

/**
 * Correct the detected domain and target role.
 *
 * The backend stores corrections alongside the detected values, so the panel
 * shows both: what was detected, what the user confirmed, and which one is
 * actually in effect.
 */
function ProfileCorrection({ resumeId, profile, onUpdated }) {
  const [domain, setDomain] = useState(profile.confirmed_domain ?? '')
  const [role, setRole] = useState(profile.confirmed_target_role ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)

  const unchanged =
    domain === (profile.confirmed_domain ?? '') && role === (profile.confirmed_target_role ?? '')

  async function submit(event) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      const payload = {}
      // Only send what the user actually filled in; the backend requires at
      // least one of the two.
      if (domain.trim()) payload.confirmedDomain = domain.trim()
      if (role.trim()) payload.confirmedTargetRole = role.trim()

      const { data, message: responseMessage } = await updateProfile(resumeId, payload)
      onUpdated(data)
      setMessage(responseMessage ?? 'Profile updated.')
    } catch (submitError) {
      setError(submitError)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card
      title="Profile correction"
      subtitle="Stored alongside the detection, never over it. The resume file is not modified."
    >
      <div className="compare-grid">
        <div>
          <h3 className="section-heading">Detected</h3>
          <Field label="Domain">{orDash(profile.domain?.key)}</Field>
          <Field label="Target role">{orDash(profile.target_role?.key)}</Field>
        </div>
        <div>
          <h3 className="section-heading">Confirmed</h3>
          <Field label="Domain">{orDash(profile.confirmed_domain)}</Field>
          <Field label="Target role">{orDash(profile.confirmed_target_role)}</Field>
          <Field label="Confirmed at">{orDash(formatDateTime(profile.confirmed_at))}</Field>
        </div>
        <div>
          <h3 className="section-heading">In effect</h3>
          <Field label="Domain">{orDash(profile.effective_domain)}</Field>
          <Field label="Target role">{orDash(profile.effective_target_role)}</Field>
          <Field label="Status">
            {profile.is_confirmed ? (
              <Badge tone="good">User confirmed</Badge>
            ) : (
              <Badge tone="neutral">Detection only</Badge>
            )}
          </Field>
        </div>
      </div>

      <form className="form-row" onSubmit={submit}>
        <label className="input-group">
          <span>Confirmed domain</span>
          <input
            value={domain}
            onChange={(event) => setDomain(event.target.value)}
            placeholder={profile.domain?.key ?? 'e.g. finance'}
          />
          {error?.fieldError?.('body.confirmed_domain') && (
            <span className="input-error">{error.fieldError('body.confirmed_domain')}</span>
          )}
        </label>

        <label className="input-group">
          <span>Confirmed target role</span>
          <input
            value={role}
            onChange={(event) => setRole(event.target.value)}
            placeholder={profile.target_role?.key ?? 'e.g. financial_analyst'}
          />
          {error?.fieldError?.('body.confirmed_target_role') && (
            <span className="input-error">{error.fieldError('body.confirmed_target_role')}</span>
          )}
        </label>

        <Button type="submit" disabled={saving || unchanged || (!domain.trim() && !role.trim())}>
          {saving ? 'Saving…' : 'Save correction'}
        </Button>
      </form>

      <p className="muted small">
        Use the backend's own keys (for example <code>finance</code>,{' '}
        <code>financial_analyst</code>). An unknown key is rejected with a 422.
      </p>

      {message && <p className="notice notice-good">{message}</p>}
      {error && <ErrorState error={error} />}
    </Card>
  )
}
