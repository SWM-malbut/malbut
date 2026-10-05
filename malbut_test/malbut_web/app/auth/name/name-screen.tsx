"use client";

import { DisplayNameForm } from "../../components/display-name-form";

/** 처음 로그인: "어떻게 불러 드릴까요?" (목업 Name). */
export function NameScreen({ initialName, returnTo }: { initialName: string; returnTo: string }) {
  return (
    <main className="ui-login ui-name-screen">
      <header className="ui-login-head">
        <h1>어떻게 불러 드릴까요?</h1>
        <p>함께 보는 다른 보호자에게 이 이름으로 보여요. 사건에 남긴 의견이나 처리한 사람도 이 이름으로 표시돼요.</p>
      </header>
      <DisplayNameForm
        initialName={initialName}
        submitLabel="시작하기"
        onSaved={() => window.location.replace(returnTo)}
      />
      <p className="ui-login-foot">카카오·네이버·구글에서는 로그인 확인만 받고, 이름이나 이메일은 가져오지 않아요.</p>
    </main>
  );
}
