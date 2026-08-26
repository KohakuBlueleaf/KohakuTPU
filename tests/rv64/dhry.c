/* Dhrystone 2.1 for SysCore, self-contained.
 *
 * The standard benchmark, cut down only where it needs an OS: no time(), no
 * scanf, no printf -- the loop count is compiled in and the timing comes from
 * the harness's cycle count, which is exact rather than sampled.
 *
 * DMIPS/MHz = (runs / (cycles/freq)) / 1757, and 1757 is the VAX 11/780's
 * Dhrystones per second, which is what makes the unit a *relative* one.
 */

/* The standalone harness puts the console here; rv64_sys_pe puts it at
 * CTRL_BASE+8, so the node build overrides it. */
#ifndef CONSOLE_ADDR
#define CONSOLE_ADDR 0x10000000ull
#endif
#define CONSOLE ((volatile unsigned char *)CONSOLE_ADDR)
static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void puts_(const char *s) { while (*s) putch(*s++); }
static void put_u64(unsigned long long v) {
    char b[21];
    int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) putch(b[--n]);
}

typedef enum { Ident_1, Ident_2, Ident_3, Ident_4, Ident_5 } Enumeration;
typedef int One_Thirty;
typedef int One_Fifty;
typedef char Capital_Letter;
typedef int Boolean;
typedef char Str_30[31];
typedef int Arr_1_Dim[50];
typedef int Arr_2_Dim[50][50];

typedef struct record {
    struct record *Ptr_Comp;
    Enumeration Discr;
    union {
        struct {
            Enumeration Enum_Comp;
            int Int_Comp;
            char Str_Comp[31];
        } var_1;
        struct { Enumeration E_Comp_2; char Str_2_Comp[31]; } var_2;
        struct { char Ch_1_Comp; char Ch_2_Comp; } var_3;
    } variant;
} Rec_Type, *Rec_Pointer;

static Rec_Pointer Ptr_Glob, Next_Ptr_Glob;
static int Int_Glob;
static Boolean Bool_Glob;
static char Ch_1_Glob, Ch_2_Glob;
static Arr_1_Dim Arr_1_Glob;
static Arr_2_Dim Arr_2_Glob;
static Rec_Type rec_a, rec_b;

static void strcpy_(char *d, const char *s) { while ((*d++ = *s++)) ; }
static int strcmp_(const char *a, const char *b) {
    while (*a && *a == *b) { ++a; ++b; }
    return (unsigned char)*a - (unsigned char)*b;
}

static Boolean Func_3(Enumeration Enum_Par_Val) {
    return Enum_Par_Val == Ident_3;
}

static void Proc_7(One_Fifty a, One_Fifty b, One_Fifty *c) { *c = a + b + 2; }

static void Proc_8(Arr_1_Dim A, Arr_2_Dim B, One_Fifty c, One_Fifty d) {
    One_Fifty i = c + 5;
    A[i] = d;
    A[i + 1] = A[i];
    A[i + 30] = i;
    for (One_Fifty j = i; j <= i + 1; ++j) B[i][j] = i;
    B[i][i - 1] += 1;
    B[i + 20][i] = A[i];
    Int_Glob = 5;
}

static Enumeration Func_1(Capital_Letter a, Capital_Letter b) {
    Capital_Letter x = a, y = b;
    return (x != y) ? Ident_1 : Ident_2;
}

static Boolean Func_2(Str_30 s1, Str_30 s2) {
    One_Thirty i = 1;
    Capital_Letter c = ' ';
    while (i <= 1) {
        if (Func_1(s1[i], s2[i + 1]) == Ident_1) { c = 'A'; ++i; }
    }
    if (c >= 'W' && c < 'Z') i = 7;
    if (c == 'X') return 1;
    if (strcmp_(s1, s2) > 0) { i += 7; Int_Glob = i; return 1; }
    return 0;
}

static void Proc_6(Enumeration in, Enumeration *out) {
    *out = in;
    if (!Func_3(in)) *out = Ident_4;
    switch (in) {
        case Ident_1: *out = Ident_1; break;
        case Ident_2: *out = (Int_Glob > 100) ? Ident_1 : Ident_4; break;
        case Ident_3: *out = Ident_2; break;
        case Ident_4: break;
        case Ident_5: *out = Ident_3; break;
    }
}

static void Proc_3(Rec_Pointer *p) {
    if (Ptr_Glob != 0) *p = Ptr_Glob->Ptr_Comp;
    Proc_7(10, Int_Glob, &Ptr_Glob->variant.var_1.Int_Comp);
}

static void Proc_5(void) { Ch_1_Glob = 'A'; Bool_Glob = 0; }

static void Proc_4(void) {
    Boolean b = (Ch_1_Glob == 'A');
    b |= Bool_Glob;
    Ch_2_Glob = 'B';
}

static void Proc_2(One_Fifty *p) {
    One_Fifty i = *p + 10;
    Enumeration e;
    do {
        if (Ch_1_Glob == 'A') { --i; *p = i - Int_Glob; e = Ident_1; }
    } while (e != Ident_1);
}

static void Proc_1(Rec_Pointer Ptr_Val_Par) {
    Rec_Pointer Next = Ptr_Val_Par->Ptr_Comp;
    *Ptr_Val_Par->Ptr_Comp = *Ptr_Glob;
    Ptr_Val_Par->variant.var_1.Int_Comp = 5;
    Next->variant.var_1.Int_Comp = Ptr_Val_Par->variant.var_1.Int_Comp;
    Next->Ptr_Comp = Ptr_Val_Par->Ptr_Comp;
    Proc_3(&Next->Ptr_Comp);
    if (Next->Discr == Ident_1) {
        Next->variant.var_1.Int_Comp = 6;
        Proc_6(Ptr_Val_Par->variant.var_1.Enum_Comp,
               &Next->variant.var_1.Enum_Comp);
        Next->Ptr_Comp = Ptr_Glob->Ptr_Comp;
        Proc_7(Next->variant.var_1.Int_Comp, 10,
               &Next->variant.var_1.Int_Comp);
    } else {
        *Ptr_Val_Par = *Ptr_Val_Par->Ptr_Comp;
    }
}

#ifndef RUNS
#define RUNS 500
#endif

int main(void) {
    Str_30 Str_1_Loc, Str_2_Loc;
    One_Fifty Int_1_Loc, Int_2_Loc, Int_3_Loc;
    Capital_Letter Ch_Index;
    Enumeration Enum_Loc;
    int Ch_1_Loc, Ch_2_Loc;

    Next_Ptr_Glob = &rec_a;
    Ptr_Glob = &rec_b;
    Ptr_Glob->Ptr_Comp = Next_Ptr_Glob;
    Ptr_Glob->Discr = Ident_1;
    Ptr_Glob->variant.var_1.Enum_Comp = Ident_3;
    Ptr_Glob->variant.var_1.Int_Comp = 40;
    strcpy_(Ptr_Glob->variant.var_1.Str_Comp,
            "DHRYSTONE PROGRAM, SOME STRING");
    strcpy_(Str_1_Loc, "DHRYSTONE PROGRAM, 1'ST STRING");
    Arr_2_Glob[8][7] = 10;

    puts_("Dhrystone 2.1, runs=");
    put_u64(RUNS);
    putch('\n');

    for (int Run_Index = 1; Run_Index <= RUNS; ++Run_Index) {
        Proc_5();
        Proc_4();
        Int_1_Loc = 2;
        Int_2_Loc = 3;
        strcpy_(Str_2_Loc, "DHRYSTONE PROGRAM, 2'ND STRING");
        Enum_Loc = Ident_2;
        Bool_Glob = !Func_2(Str_1_Loc, Str_2_Loc);
        while (Int_1_Loc < Int_2_Loc) {
            Int_3_Loc = 5 * Int_1_Loc - Int_2_Loc;
            Proc_7(Int_1_Loc, Int_2_Loc, &Int_3_Loc);
            ++Int_1_Loc;
        }
        Proc_8(Arr_1_Glob, Arr_2_Glob, Int_1_Loc, Int_3_Loc);
        Proc_1(Ptr_Glob);
        for (Ch_Index = 'A'; Ch_Index <= Ch_2_Glob; ++Ch_Index) {
            if (Enum_Loc == Func_1(Ch_Index, 'C')) {
                Proc_6(Ident_1, &Enum_Loc);
                strcpy_(Str_2_Loc, "DHRYSTONE PROGRAM, 3'RD STRING");
                Int_2_Loc = Run_Index;
                Int_Glob = Run_Index;
            }
        }
        Int_2_Loc = Int_2_Loc * Int_1_Loc;
        Int_1_Loc = Int_2_Loc / Int_3_Loc;
        Int_2_Loc = 7 * (Int_2_Loc - Int_3_Loc) - Int_1_Loc;
        Proc_2(&Int_1_Loc);
    }

    /* Print the standard finals so a wrong answer is visible, not silent. */
    puts_("Int_Glob=");      put_u64((unsigned long long)Int_Glob);
    puts_(" Bool_Glob=");    put_u64((unsigned long long)Bool_Glob);
    puts_(" Ch_1=");         put_u64((unsigned long long)Ch_1_Glob);
    puts_(" Ch_2=");         put_u64((unsigned long long)Ch_2_Glob);
    puts_("\nArr_1[8]=");    put_u64((unsigned long long)Arr_1_Glob[8]);
    puts_(" Arr_2[8][7]=");  put_u64((unsigned long long)Arr_2_Glob[8][7]);
    puts_("\nPtr_Glob Int="); put_u64((unsigned long long)Ptr_Glob->variant.var_1.Int_Comp);
    puts_("\nInt_1=");       put_u64((unsigned long long)Int_1_Loc);
    puts_(" Int_2=");        put_u64((unsigned long long)Int_2_Loc);
    puts_(" Int_3=");        put_u64((unsigned long long)Int_3_Loc);
    putch('\n');

    /* The reference finals for Dhrystone 2.1. */
    int ok = (Int_Glob == 5) && (Bool_Glob == 1) && (Ch_1_Glob == 'A') &&
             (Ch_2_Glob == 'B') && (Arr_1_Glob[8] == 7) &&
             (Arr_2_Glob[8][7] == RUNS + 10) && (Int_1_Loc == 5) &&
             (Int_2_Loc == 13) && (Int_3_Loc == 7);
    puts_(ok ? "dhrystone ok\n" : "DHRYSTONE WRONG\n");
    return ok ? 0 : 1;
}
