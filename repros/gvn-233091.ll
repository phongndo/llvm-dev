; https://github.com/llvm/llvm-project/issues/233091
; RUN: opt -passes=gvn -S %s
; Expected: no `inbounds` leader feeding %r's PRE copy (alive: target more poisonous)
define i64 @test(i1 %c, i64 %idx) {
entry:
  br i1 %c, label %pA, label %pB
pA:
  %ga = getelementptr i8, ptr null, i64 %idx
  %ra = ptrtoint ptr %ga to i64
  br label %join
pB:
  %gb = getelementptr inbounds i8, ptr null, i64 %idx
  br label %join
join:
  %g = getelementptr i8, ptr null, i64 %idx
  %r = ptrtoint ptr %g to i64
  ret i64 %r
}
