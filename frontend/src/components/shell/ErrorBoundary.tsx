import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ShellErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[harmonograf] shell error boundary caught:', error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <section className="hg-panel" data-testid="shell-error-boundary">
          <header className="hg-panel__header">
            <h2 className="hg-panel__title">Something went wrong</h2>
          </header>
          <div className="hg-panel__body">
            <div className="hg-panel__empty">
              <p>{this.state.error.message || String(this.state.error)}</p>
              <button type="button" onClick={this.reset}>
                Retry
              </button>
            </div>
          </div>
        </section>
      );
    }
    return this.props.children;
  }
}
