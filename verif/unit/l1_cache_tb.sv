`default_nettype none
// replay core operations into l1_cache against a randomly stalling line memory.
// records responses, flush/reset completions and memory traffic for tests/test_l1_cache_rtl.py.
// stimulus, one per line (hex): l addr | s addr data mask | u addr | w addr value | f | z addr
//   | i line_addr data (preload memory) | e r|w line_addr (fill/writeback error) | x mode (corrupt the protocol, see bad_mode).
module l1_cache_tb #(
  parameter bit WRITABLE = 1'b1
);
  import memory_protocol_pkg::*;

  logic clk = 1'b0, rst = 1'b1;
  always #5 clk = ~clk;

  logic creq_valid = 1'b0, crsp_ready = 1'b0, mreq_ready = 1'b0, mrsp_valid = 1'b0, flush_valid = 1'b0;
  logic unsol_core = 1'b0, unsol_mem = 1'b0;
  mem_request_t creq = '0, mreq;
  mem_response_t mrsp = '0, crsp, pend, mrsp_in;
  wire creq_ready, crsp_valid, mreq_valid, mrsp_ready, flush_ready, busy, fatal;
  wire [1:0] fatal_reason;
  // an unsolicited response: valid while the cache is not waiting for one.
  wire unsol = unsol_core || unsol_mem;
  always_comb begin
    mrsp_in = mrsp;
    if (unsol) begin
      mrsp_in = '0;
      mrsp_in.transaction_id = 4'hf;
    end
  end

  l1_cache #(.WRITABLE(WRITABLE)) dut (
    .clk_i(clk), .rst_i(rst),
    .core_request_valid_i(creq_valid), .core_request_ready_o(creq_ready), .core_request_i(creq),
    .core_response_valid_o(crsp_valid), .core_response_ready_i(crsp_ready), .core_response_o(crsp),
    .mem_request_valid_o(mreq_valid), .mem_request_ready_i(mreq_ready), .mem_request_o(mreq),
    .mem_response_valid_i(mrsp_valid || unsol), .mem_response_ready_o(mrsp_ready), .mem_response_i(mrsp_in),
    .flush_valid_i(flush_valid), .flush_ready_o(flush_ready), .busy_o(busy),
    .fatal_o(fatal), .fatal_reason_o(fatal_reason));

  string stim_path, out_path;
  int stim, out, seed;

  task automatic fail(string why);
    $fdisplay(out, "ERR %s", why);
    $fclose(out);
    $finish;
  endtask

  // backing memory, by line address; unwritten lines read as zero.
  logic [255:0] memory [int unsigned];
  bit read_error [int unsigned], write_error [int unsigned];

  // bad_mode: 1 wrong id on the next memory response, 2 uncached data on the next fill,
  // 3 unsolicited response while the core response is offered,
  // 4 unsolicited response while a memory request is offered.
  int bad_mode = 0;
  bit hold_mem = 1'b0, resetting = 1'b0;

  typedef enum {D_NEXT, D_REQ, D_RSP, D_FLUSH, D_HOLD, D_RESET, D_FATAL} drive_e;
  drive_e ds = D_NEXT;
  int count = 0, idle = 0;
  logic [3:0] id = '0;
  byte cmd, kind;
  logic [31:0] a, m, v;
  logic [255:0] d;
  int mode;
  mem_request_t r;

  initial begin
    if (!$value$plusargs("stim=%s", stim_path) || !$value$plusargs("out=%s", out_path)
        || !$value$plusargs("seed=%d", seed))
      $fatal(1, "need +stim= +out= +seed=");
    stim = $fopen(stim_path, "r");
    out = $fopen(out_path, "w");
    void'($urandom(seed));
    repeat (3) @(posedge clk);
    rst <= 1'b0;
  end

  // core side: one operation at a time, response ready stalls at random.
  always @(posedge clk) begin
    if (rst && (creq_ready || crsp_valid || mreq_valid || mrsp_ready || flush_ready))
      fail("handshake_in_reset");
    if (unsol && ((creq_valid && creq_ready) || (crsp_valid && crsp_ready)
                  || (mreq_valid && mreq_ready) || (flush_valid && flush_ready)))
      fail("handshake_with_bad_response");
    idle <= idle + 1;
    if (idle > 5000) fail("timeout");
    if (fatal && ds != D_FATAL) begin
      ds <= D_FATAL;
      count <= 0;
      creq_valid <= 1'b0;
      crsp_ready <= 1'b0;
      flush_valid <= 1'b0;
    end else unique case (ds)
      D_NEXT: if (!rst) begin
        idle <= 0;
        if ($fscanf(stim, " %c", cmd) != 1) begin
          $fdisplay(out, "DONE");
          $fclose(out);
          $finish;
        end
        r = '0;
        r.transaction_id = id;
        unique case (cmd)
          "l", "z": begin
            void'($fscanf(stim, "%h", a));
            r.address = a;
            if (cmd == "z") begin
              r.uncached = 1'b1;
              r.uncached_size = MEM_SIZE_WORD;
              hold_mem <= 1'b1;
              resetting <= 1'b1;
            end
          end
          "s": begin
            void'($fscanf(stim, "%h %h %h", a, d, m));
            r.write = 1'b1;
            r.address = a;
            r.line_write_data = d;
            r.line_write_mask = m;
          end
          "u", "w": begin
            void'($fscanf(stim, "%h", a));
            r.uncached = 1'b1;
            r.address = a;
            r.uncached_size = MEM_SIZE_WORD;
            if (cmd == "w") begin
              void'($fscanf(stim, "%h", v));
              r.write = 1'b1;
              r.uncached_write_data = v;
              r.uncached_write_strobe = 4'hf;
            end
          end
          "f": ;
          "e": begin
            void'($fscanf(stim, " %c %h", kind, a));
            if (kind == "r") read_error[a] = 1'b1;
            else write_error[a] = 1'b1;
          end
          "i": begin
            void'($fscanf(stim, "%h %h", a, d));
            memory[a] = d;
          end
          "x": void'($fscanf(stim, "%d", mode));
          default: fail("bad_stimulus");
        endcase
        if (cmd == "x") bad_mode <= mode;
        else if (cmd == "f") begin
          flush_valid <= 1'b1;
          ds <= D_FLUSH;
        end else if (cmd != "e" && cmd != "i") begin
          creq <= r;
          creq_valid <= 1'b1;
          id <= id + 1'b1;
          ds <= D_REQ;
        end
      end
      D_REQ: if (creq_valid && creq_ready) begin
        creq_valid <= 1'b0;
        count <= 0;
        ds <= resetting ? D_HOLD : D_RSP;
      end
      D_RSP:
        if (crsp_valid && crsp_ready) begin
          if (crsp.transaction_id != creq.transaction_id) fail("response_id");
          $fdisplay(out, "R %0d %h %h", crsp.status, crsp.line_read_data, crsp.uncached_read_data);
          crsp_ready <= 1'b0;
          ds <= D_NEXT;
        end else if (crsp_valid && bad_mode == 3) begin
          unsol_core <= 1'b1;
          crsp_ready <= 1'b1;
        end else crsp_ready <= bad_mode != 3 && $urandom % 3 != 0;
      D_FLUSH: if (flush_valid && flush_ready) begin
        flush_valid <= 1'b0;
        $fdisplay(out, "F");
        ds <= D_NEXT;
      end
      // an uncached request is held in flight by memory, then reset arrives.
      D_HOLD: begin
        count <= count + 1;
        if (count == 3) begin
          if (!mreq_valid) fail("hold_not_in_flight");
          rst <= 1'b1;
          count <= 0;
          ds <= D_RESET;
        end
      end
      D_RESET: begin
        count <= count + 1;
        if (count == 3) begin
          rst <= 1'b0;
          hold_mem <= 1'b0;
          resetting <= 1'b0;
          $fdisplay(out, "Z");
          ds <= D_NEXT;
        end
      end
      // a later bad response must not replace the first fatal reason.
      D_FATAL: begin
        count <= count + 1;
        unsol_core <= count == 0;
        if (count == 3) begin
          $fdisplay(out, "FATAL %0d", fatal_reason);
          $fclose(out);
          $finish;
        end
      end
    endcase
  end

  // memory side: random request ready, 1-3 cycle response delay, error injection.
  typedef enum {R_IDLE, R_WAIT, R_RSP} respond_e;
  respond_e rs = R_IDLE;
  int delay = 0;
  int unsigned line;

  always @(posedge clk) begin
    if (rst) begin
      rs <= R_IDLE;
      mreq_ready <= 1'b0;
      mrsp_valid <= 1'b0;
      mrsp <= '0;
      unsol_mem <= 1'b0;
    end else unique case (rs)
      R_IDLE: begin
        unsol_mem <= 1'b0;
        if (mreq_valid && mreq_ready) begin
          line = mreq.address >> 5;
          pend = '0;
          pend.transaction_id = mreq.transaction_id;
          if (mreq.uncached) begin
            if (mreq != creq) fail("uncached_not_passed_through");
            if (!mreq.write) pend.uncached_read_data = mreq.address ^ 32'h5a5a5a5a;
          end else if (mreq.write) begin
            $fdisplay(out, "M wb %h %h", mreq.address, mreq.line_write_data);
            if (mreq.line_write_mask != '1 || mreq.address[4:0] != 0) fail("bad_writeback");
            if (write_error.exists(line)) pend.status = MEM_STATUS_ACCESS_FAULT;
            else memory[line] = mreq.line_write_data;
          end else begin
            $fdisplay(out, "M fill %h", mreq.address);
            if (read_error.exists(line)) pend.status = MEM_STATUS_ACCESS_FAULT;
            else pend.line_read_data = memory.exists(line) ? memory[line] : '0;
            if (bad_mode == 2) pend.uncached_read_data = 32'h1;
          end
          if (bad_mode == 1) pend.transaction_id = pend.transaction_id ^ 4'h1;
          mreq_ready <= 1'b0;
          delay <= $urandom % 3;
          rs <= R_WAIT;
        end else if (mreq_valid && bad_mode == 4) begin
          mreq_ready <= 1'b1;
          unsol_mem <= 1'b1;
        end else mreq_ready <= !hold_mem && bad_mode != 4 && $urandom % 4 != 0;
      end
      R_WAIT:
        if (delay == 0) begin
          mrsp <= pend;
          mrsp_valid <= 1'b1;
          rs <= R_RSP;
        end else delay <= delay - 1;
      R_RSP: if (mrsp_valid && mrsp_ready) begin
        mrsp_valid <= 1'b0;
        mrsp <= '0;
        rs <= R_IDLE;
      end
    endcase
  end
endmodule
`default_nettype wire
