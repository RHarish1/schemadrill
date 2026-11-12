import { useEffect, useState } from "react";
import { ApiRequestError, validateLlmKey } from "../services/api";
import {
  clearLlmKey,
  getAvailableLlmModels,
  getKeyLast4,
  getLlmProvider,
  getSelectedFallbackModel,
  getSelectedLlmModel,
  setLlmKeySession,
  updateLlmModelSelection,
} from "../llmKey";

export default function SettingsModal({ open, onClose, initialError = null }) {
  const [provider, setProvider] = useState(() => getLlmProvider());
  const [keyInput, setKeyInput] = useState("");
  const [validating, setValidating] = useState(false);
  const [validationError, setValidationError] = useState(initialError);
  const [validationSuccess, setValidationSuccess] = useState(null);

  const activeLast4 = getKeyLast4();
  const models = getAvailableLlmModels();
  const selectedModel = getSelectedLlmModel();
  const selectedFallback = getSelectedFallbackModel();

  useEffect(() => {
    if (open) {
      setProvider(getLlmProvider());
      setKeyInput("");
      setValidationError(initialError);
      setValidationSuccess(null);
    }
  }, [open, initialError]);

  if (!open) return null;

  async function handleValidate(event) {
    event.preventDefault();
    const trimmed = keyInput.trim();
    if (!trimmed || validating) return;
    setValidating(true);
    setValidationError(null);
    setValidationSuccess(null);
    try {
      const response = await validateLlmKey(trimmed, provider);
      if (!response.valid || !response.models.length) {
        setValidationError("No generateContent-capable models found for this key.");
        return;
      }
      setLlmKeySession({
        apiKey: trimmed,
        provider,
        models: response.models,
      });
      setKeyInput("");
      setValidationSuccess("Key validated for this browser tab.");
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        onClose();
        return;
      }
      setValidationError(
        err instanceof ApiRequestError
          ? err.message
          : "Could not validate your API key. Please try again.",
      );
    } finally {
      setValidating(false);
    }
  }

  function handleClearKey() {
    clearLlmKey();
    setKeyInput("");
    setValidationError(null);
    setValidationSuccess(null);
  }

  function handleModelChange(event) {
    updateLlmModelSelection(event.target.value, selectedFallback);
  }

  function handleFallbackChange(event) {
    updateLlmModelSelection(selectedModel, event.target.value);
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section
        className="settings-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="settings-modal-head">
          <div>
            <h2 id="settings-modal-title">LLM API Key Settings</h2>
            <p className="settings-modal-sub">
              Bring your own Gemini key to use your own quota and pick models.
            </p>
          </div>
          <button
            type="button"
            className="settings-close-btn"
            onClick={onClose}
            aria-label="Close settings"
          >
            ✕
          </button>
        </header>

        <div className="settings-security-note">
          Your key is held only in this tab&apos;s memory and <code>sessionStorage</code> (it
          survives page refresh and isn&apos;t shared with new tabs). It is never stored on the
          server or logged, and is cleared on <strong>Clear key</strong> or sign-out (browsers may
          still restore a closed tab&apos;s <code>sessionStorage</code> if you reopen it).
        </div>

        {activeLast4 ? (
          <div className="settings-active-key">
            <span className="settings-key-badge">
              Key ending •••• {activeLast4}
            </span>
            <button
              type="button"
              className="settings-clear-btn"
              onClick={handleClearKey}
            >
              Clear key
            </button>
          </div>
        ) : null}

        <form className="settings-form" onSubmit={handleValidate} autoComplete="off">
          <label className="settings-field">
            <span>Provider</span>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              disabled={validating}
            >
              <option value="gemini">Google Gemini</option>
            </select>
          </label>

          <label className="settings-field">
            <span>{activeLast4 ? "Replace Gemini API key" : "Gemini API key"}</span>
            <div className="settings-key-row">
              <input
                type="password"
                value={keyInput}
                onChange={(e) => setKeyInput(e.target.value)}
                placeholder="Paste your Gemini API key (AIza...)"
                autoComplete="off"
                spellCheck={false}
                data-lpignore="true"
                data-1p-ignore="true"
                disabled={validating}
              />
              <button
                type="submit"
                className="settings-validate-btn"
                disabled={!keyInput.trim() || validating}
              >
                {validating ? "Validating..." : "Validate"}
              </button>
            </div>
          </label>
        </form>

        {validationError && (
          <div className="settings-feedback settings-feedback--error" role="alert">
            {validationError}
          </div>
        )}
        {validationSuccess && (
          <div className="settings-feedback settings-feedback--ok" role="status">
            {validationSuccess}
          </div>
        )}

        {activeLast4 && models.length > 0 && (
          <div className="settings-models">
            <label className="settings-field">
              <span>Primary model</span>
              <select value={selectedModel} onChange={handleModelChange}>
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.display_name} ({m.id})
                  </option>
                ))}
              </select>
            </label>

            <label className="settings-field">
              <span>Fallback model (optional)</span>
              <select value={selectedFallback} onChange={handleFallbackChange}>
                <option value="">None</option>
                {models
                  .filter((m) => m.id !== selectedModel)
                  .map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.display_name} ({m.id})
                    </option>
                  ))}
              </select>
            </label>
          </div>
        )}
      </section>
    </div>
  );
}
