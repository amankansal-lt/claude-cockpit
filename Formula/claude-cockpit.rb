class ClaudeCockpit < Formula
  desc "X-ray vision for your Claude Code brain — TUI dashboard"
  homepage "https://github.com/AmanKansal2012/claude-cockpit"
  url "https://github.com/AmanKansal2012/claude-cockpit/releases/download/v1.0.0/cockpit-1.0.0-macos-arm64.zip"
  sha256 "b2effe873fe9444fa4287f31ee213e934dcd381028fa273bcb4d196e7ecd5551"
  license "MIT"
  version "1.0.0"

  depends_on arch: :arm64

  def install
    bin.install "cockpit"
  end

  test do
    assert_match "Claude Cockpit", shell_output("#{bin}/cockpit --version")
  end
end
