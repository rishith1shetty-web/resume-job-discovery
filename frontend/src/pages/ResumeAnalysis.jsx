/**
 * Resume Analysis.
 *
 * Presents the structured profile the backend extracted from the active
 * resume. Nothing on this screen is computed from the resume text here — the
 * extraction is deterministic and lives in the backend, and a second opinion
 * rendered in the browser could only disagree with the thing that actually
 * drives matching.
 *
 * The one derived figure is "match readiness", and it is explicitly a
 * completeness check on the profile rather than a prediction: it counts which
 * inputs the scorer needs and which of them this resume actually supplied. It
 * is labelled as such on screen so it cannot be mistaken for a match score.
 */

import { useState } from 'react'

import { ApiError } from '../api/client.js'
import { analyzeResume, getProfile, updateProfile } from '../api/endpoints.js'
import { Icon } from '../components/Icon.jsx'
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
  Meter,
  Notice,
  PageHeader,
  Skeleton,
  StatCard,
  formatDateTime,
  humanize,
  orDash,
} from '../components/ui.jsx'

export function ResumeAnalysis({ navigate }) {
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
      <div className="stack">
        <PageHeader title="Resume Analysis" />
        <Card>
          <EmptyState
            icon="document"
            title="No active resume"
            hint="Analysis is always scoped to a single resume. Choose one in Resume Management first."
          >
            <Button variant="primary" icon="document" onClick={() => navigate('resumes')}>
              Go to Resume Management
            </Button>
          </EmptyState>
        </Card>
      </div>
    )
  }

  // A resume that has never been analysed returns 404 from GET /profile. That
  // is an expected state here, not a failure to shout about.
  const notAnalyzedYet = profile.error instanceof ApiError && profile.error.status === 404
  const busy = analyzing || profile.loading

  return (
    <div className="stack">
      <PageHeader
        title="Resume Analysis"
        lede={activeResume?.original_filename ?? 'Active resume'}
        actions={
          <Button variant="primary" icon="insight" onClick={runAnalysis} disabled={analyzing}>
            {analyzing ? 'Analysing…' : profile.data ? 'Re-run analysis' : 'Run analysis'}
          </Button>
        }
      />

      {analyzeMessage && <Notice tone="warn">{analyzeMessage}</Notice>}
      {analyzeError && <ErrorState error={analyzeError} />}

      {analyzing && (
        <Card>
          <div className="progress-rail" />
          <p className="muted small">
            Extracting the structured profile. Deterministic — no model call, no network.
          </p>
          <Skeleton count={4} height={16} />
        </Card>
      )}

      {!analyzing && profile.loading && (
        <div className="grid grid-stats">
          <Skeleton variant="card" count={4} height={120} />
        </div>
      )}

      {!analyzing && !profile.loading && notAnalyzedYet && (
        <Card>
          <EmptyState
            icon="insight"
            title="This resume has not been analysed yet"
            hint="Run the analysis to detect one primary domain and one target role. The resume file itself is never modified."
          >
            <Button variant="primary" icon="insight" onClick={runAnalysis}>
              Run analysis
            </Button>
          </EmptyState>
        </Card>
      )}

      {!analyzing && !profile.loading && profile.error && !notAnalyzedYet && (
        <ErrorState error={profile.error} onRetry={profile.run} />
      )}

      {!busy && profile.data && (
        <ProfileView
          profile={profile.data}
          resumeId={activeResumeId}
          navigate={navigate}
          onUpdated={(updated) => profile.setData(updated)}
        />
      )}
    </div>
  )
}

/**
 * Which inputs the scorer relies on, and whether this resume supplied them.
 *
 * Presented as a checklist rather than a single opaque percentage so the user
 * can see exactly what is missing and fix it, instead of being told a number
 * they cannot act on.
 */
function readinessChecks(profile) {
  const skills = profile.skills ?? []
  return [
    {
      label: 'Target role detected',
      ok: Boolean(profile.effective_target_role),
      detail: 'Decides which listings are eligible at all.',
    },
    {
      label: 'Primary domain detected',
      ok: Boolean(profile.effective_domain),
      detail: 'Keeps unrelated careers out of the results.',
    },
    {
      label: 'Skills extracted',
      ok: skills.length >= 5,
      detail: `${skills.length} found — mandatory-skill coverage is 25% of the score.`,
    },
    {
      label: 'Seniority established',
      ok: Boolean(profile.seniority),
      detail: 'Filters out roles far above or below this level.',
    },
    {
      label: 'Years of experience stated',
      ok: profile.years_experience != null,
      detail: 'Read from the resume; never assumed when absent.',
    },
    {
      label: 'Education recorded',
      ok: (profile.education ?? []).length > 0,
      detail: 'Used where a listing requires a specific qualification.',
    },
  ]
}

function ProfileView({ profile, resumeId, navigate, onUpdated }) {
  const skills = profile.skills ?? []
  const byKind = (kind) => skills.filter((skill) => skill.kind === kind)
  const checks = readinessChecks(profile)
  const met = checks.filter((check) => check.ok).length
  const readiness = Math.round((met / checks.length) * 100)

  return (
    <div className="stack">
      <section className="grid grid-stats">
        <StatCard
          label="Target role"
          value={orDash(humanize(profile.effective_target_role))}
          hint={profile.is_confirmed ? 'Confirmed by you' : 'Detected from the resume'}
          icon="target"
          text
        />
        <StatCard
          label="Domain"
          value={orDash(humanize(profile.effective_domain))}
          hint="Searches never leave this domain"
          icon="layers"
          text
        />
        <StatCard
          label="Seniority"
          value={orDash(humanize(profile.seniority))}
          hint={
            profile.years_experience == null
              ? 'Years not stated on the resume'
              : `${profile.years_experience} years of experience`
          }
          icon="trending"
          text
        />
        <StatCard
          label="Skills extracted"
          value={skills.length}
          hint={`${byKind('technology').length} technologies · ${byKind('tool').length} tools`}
          icon="sparkle"
        />
      </section>

      <div className="grid grid-2">
        <Card
          title="Match readiness"
          subtitle="What the scorer needs from a resume, and what this one supplied. Not a match score."
        >
          <div className="row">
            <Meter value={readiness} showValue={false} label="Match readiness" />
            <span className="strong tabular">
              {met}/{checks.length}
            </span>
          </div>

          <ul className="stack-sm">
            {checks.map((check) => (
              <li key={check.label} className="row" style={{ alignItems: 'flex-start' }}>
                <Icon
                  name={check.ok ? 'checkCircle' : 'warning'}
                  size={16}
                  style={{
                    color: check.ok ? 'var(--success)' : 'var(--warning)',
                    flexShrink: 0,
                    marginTop: 2,
                  }}
                />
                <span style={{ minWidth: 0 }}>
                  <span className="strong small" style={{ display: 'block' }}>
                    {check.label}
                  </span>
                  <span className="xs subtle">{check.detail}</span>
                </span>
              </li>
            ))}
          </ul>
        </Card>

        <Card
          title="Resume summary"
          subtitle="The detection, its confidence and the evidence behind it."
        >
          <div className="field-grid">
            <Field label="Detected domain" hint={`Confidence ${Math.round((profile.domain?.confidence ?? 0) * 100)}%`}>
              {orDash(profile.domain?.display ?? profile.domain?.key)}
            </Field>
            <Field label="Detected role" hint={`Confidence ${Math.round((profile.target_role?.confidence ?? 0) * 100)}%`}>
              {orDash(profile.target_role?.display ?? profile.target_role?.key)}
            </Field>
            <Field label="Seniority confidence">
              <ConfidenceBar value={profile.seniority_confidence} />
            </Field>
            <Field label="Analysed at">{orDash(formatDateTime(profile.analyzed_at))}</Field>
            <Field label="Analysis version">{orDash(profile.analysis_version)}</Field>
            <Field label="Sections found">
              {(profile.sections_found ?? []).length > 0
                ? profile.sections_found.map(humanize).join(', ')
                : orDash(null)}
            </Field>
          </div>

          {(profile.warnings ?? []).length > 0 && (
            <Notice tone="warn">
              <strong>Worth checking</strong>
              <ul className="tight-list" style={{ marginTop: 6 }}>
                {profile.warnings.map((warning, index) => (
                  <li key={index}>{warning}</li>
                ))}
              </ul>
            </Notice>
          )}

          <Button variant="primary" icon="search" onClick={() => navigate('find')}>
            Find matches for this profile
          </Button>
        </Card>
      </div>

      <Card
        title="Search keywords"
        subtitle="The only titles this platform will search. Variations of the detected role — never an adjacent career."
      >
        <div className="chip-row">
          {(profile.role_variations ?? []).map((variation) => (
            <Badge key={variation} tone="accent">
              {variation}
            </Badge>
          ))}
          {(profile.role_variations ?? []).length === 0 && (
            <span className="muted small">No variations generated.</span>
          )}
        </div>
      </Card>

      <div className="grid grid-2">
        <SkillCard title="Skills" skills={byKind('skill')} icon="sparkle" />
        <SkillCard title="Technologies" skills={byKind('technology')} icon="layers" />
        <SkillCard title="Tools" skills={byKind('tool')} icon="briefcase" />
        <Card title="Experience" subtitle={`${(profile.titles ?? []).length} title(s) on the resume`}>
          {(profile.titles ?? []).length === 0 ? (
            <EmptyState icon="briefcase" title="No job titles extracted." />
          ) : (
            <ul className="stack-sm">
              {(profile.titles ?? []).map((title, index) => (
                <li key={`${title.raw}-${index}`} className="row" style={{ alignItems: 'flex-start' }}>
                  <span className="stat-icon" style={{ width: 30, height: 30 }}>
                    <Icon name="briefcase" size={14} />
                  </span>
                  <span style={{ minWidth: 0, flex: 1 }}>
                    <span className="strong small" style={{ display: 'block' }}>
                      {title.raw}
                    </span>
                    <span className="xs subtle">{humanize(title.section) ?? 'Experience'}</span>
                  </span>
                  {title.is_most_recent && <Badge tone="good">Most recent</Badge>}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid grid-2">
        <Card title="Education" subtitle={`${(profile.education ?? []).length} entry(s)`}>
          {(profile.education ?? []).length === 0 ? (
            <EmptyState icon="graduation" title="No education recorded." />
          ) : (
            <ul className="stack-sm">
              {(profile.education ?? []).map((entry, index) => (
                <li key={index} className="row" style={{ alignItems: 'flex-start' }}>
                  <span className="stat-icon" style={{ width: 30, height: 30 }}>
                    <Icon name="graduation" size={14} />
                  </span>
                  <span style={{ minWidth: 0 }}>
                    <span className="strong small" style={{ display: 'block' }}>
                      {entry.field_of_study || humanize(entry.degree_level) || entry.raw}
                    </span>
                    <span className="xs subtle">
                      {[entry.institution, entry.year].filter(Boolean).join(' · ') || entry.raw}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Certifications" subtitle={`${(profile.certifications ?? []).length} entry(s)`}>
          {(profile.certifications ?? []).length === 0 ? (
            <EmptyState icon="award" title="No certifications recorded." />
          ) : (
            <div className="chip-row">
              {(profile.certifications ?? []).map((entry, index) => (
                <Badge key={index} tone="neutral">
                  {entry.name}
                  {entry.year && <span className="chip-count">{entry.year}</span>}
                </Badge>
              ))}
            </div>
          )}
        </Card>
      </div>

      {(profile.responsibilities ?? []).length > 0 && (
        <Card
          title="Responsibilities"
          subtitle="Compared against each listing's own responsibilities — 15% of the score."
        >
          <ul className="tight-list">
            {profile.responsibilities.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </Card>
      )}

      <ProfileCorrection resumeId={resumeId} profile={profile} onUpdated={onUpdated} />
    </div>
  )
}

function SkillCard({ title, skills, icon }) {
  return (
    <Card title={title} subtitle={`${skills.length} extracted`}>
      {skills.length === 0 ? (
        <EmptyState icon={icon} title="None extracted." />
      ) : (
        <div className="chip-row">
          {skills.map((skill) => (
            <Badge
              key={skill.skill_key}
              tone="neutral"
              title={`Appears ${skill.occurrences}× · confidence ${Math.round(
                (skill.confidence ?? 0) * 100,
              )}%`}
            >
              {skill.display}
              <span className="chip-count">{skill.occurrences}</span>
            </Badge>
          ))}
        </div>
      )}
    </Card>
  )
}

/**
 * Correct the detected domain and target role.
 *
 * The backend stores corrections alongside the detection rather than over it,
 * so the original detection and its evidence survive a correction.
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
      // Only send what was actually filled in; the backend requires at least
      // one of the two.
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
      title="Correct the detection"
      subtitle="Stored alongside the detection, never over it. The resume file is not modified."
      actions={
        profile.is_confirmed ? (
          <Badge tone="good">Confirmed by you</Badge>
        ) : (
          <Badge tone="neutral">Detection only</Badge>
        )
      }
    >
      <form className="field-row" onSubmit={submit}>
        <label className="input-group">
          <span className="input-label">Domain</span>
          <input
            className="input"
            value={domain}
            onChange={(event) => setDomain(event.target.value)}
            placeholder={profile.domain?.key ?? 'e.g. finance'}
          />
          <span className="input-hint">
            In effect: <strong>{profile.effective_domain ?? '—'}</strong>
          </span>
          {error?.fieldError?.('body.confirmed_domain') && (
            <span className="input-error">{error.fieldError('body.confirmed_domain')}</span>
          )}
        </label>

        <label className="input-group">
          <span className="input-label">Target role</span>
          <input
            className="input"
            value={role}
            onChange={(event) => setRole(event.target.value)}
            placeholder={profile.target_role?.key ?? 'e.g. financial_analyst'}
          />
          <span className="input-hint">
            In effect: <strong>{profile.effective_target_role ?? '—'}</strong>
          </span>
          {error?.fieldError?.('body.confirmed_target_role') && (
            <span className="input-error">{error.fieldError('body.confirmed_target_role')}</span>
          )}
        </label>

        <Button
          type="submit"
          variant="default"
          disabled={saving || unchanged || (!domain.trim() && !role.trim())}
        >
          {saving ? 'Saving…' : 'Save correction'}
        </Button>
      </form>

      <p className="muted small">
        Use the platform&apos;s own keys — for example <code>finance</code> or{' '}
        <code>financial_analyst</code>. An unrecognised key is rejected rather than guessed at.
      </p>

      {message && <Notice tone="good">{message}</Notice>}
      {error && <ErrorState error={error} />}
    </Card>
  )
}
